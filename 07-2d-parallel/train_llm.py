import argparse
from contextlib import contextmanager
from itertools import chain
import json
import multiprocessing
import os
import time
from pathlib import Path
import logging

import torch
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch import distributed as dist
import torch.distributed.tensor.parallel as tp
from torch.distributed._tensor import Shard, Replicate
from torch.distributed.elastic.multiprocessing.errors import record
import torch.distributed.checkpoint as DCP
from torch.distributed._composable.fsdp import fully_shard

import wandb
import tqdm
import datasets
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    default_data_collator,
)

LOGGER = logging.getLogger(__name__)


@record
def main():
    parser = _get_parser()
    args = parser.parse_args()

    dist.init_process_group()

    rank = dist.get_rank()
    local_rank = rank % torch.cuda.device_count()
    world_size = dist.get_world_size()

    assert args.tp > 1
    assert world_size % args.tp == 0

    mesh = dist.device_mesh.init_device_mesh(
        "cuda",
        (world_size // args.tp, args.tp),
        mesh_dim_names=("dp", "tp"),
    )

    logging.basicConfig(
        format=f"[rank={rank}] [%(asctime)s] %(levelname)s:%(message)s",
        level=logging.INFO,
    )

    LOGGER.info(os.environ)
    LOGGER.info(args)
    LOGGER.info(f"local_rank={local_rank} rank={rank} world size={world_size}")
    LOGGER.info(f"dp_size={mesh['dp'].size()} tp_size={mesh['tp'].size()}")

    device = torch.device(f"cuda:{local_rank}")
    dtype = torch.bfloat16
    torch.cuda.set_device(device)

    torch.manual_seed(args.seed)

    LOGGER.info(f"Loading model from HF_HOME={os.environ['HF_HOME']}")

    with rank_ordered(should_go_first=local_rank == 0):
        config = AutoConfig.from_pretrained(args.model_name, use_cache=False)
        with device:
            model = AutoModelForCausalLM.from_config(
                config, torch_dtype=dtype, attn_implementation="flash_attention_2"
            )
    LOGGER.info(f"{sum(p.numel() for p in model.parameters())} model parameters")

    tp.parallelize_module(
        model,
        mesh["tp"],
        {"model.embed_tokens": tp.ColwiseParallel(output_layouts=Shard(1))},
    )
    for layer in model.model.layers:
        tp.parallelize_module(
            layer,
            mesh["tp"],
            {
                # SequenceParallel will apply sharding to sequence dimension.
                "input_layernorm": tp.SequenceParallel(),
                # The input to self_attn (which is the output from the SequenceParallel input_layer_norm) will be sharded on dimension 1, but we wanted it to be the whole tensor.
                "self_attn": tp.PrepareModuleInput(
                    input_kwarg_layouts={"hidden_states": Shard(dim=1)},
                    desired_input_kwarg_layouts={"hidden_states": Replicate()},
                ),
                "self_attn.q_proj": tp.ColwiseParallel(),
                "self_attn.k_proj": tp.ColwiseParallel(),
                "self_attn.v_proj": tp.ColwiseParallel(),
                "self_attn.o_proj": tp.RowwiseParallel(output_layouts=Shard(1)),
                # Another sharding along sequence dimension.
                "post_attention_layernorm": tp.SequenceParallel(),
                "mlp": tp.PrepareModuleInput(
                    input_layouts=Shard(dim=1),
                    desired_input_layouts=Replicate(),
                ),
                "mlp.gate_proj": tp.ColwiseParallel(),
                "mlp.up_proj": tp.ColwiseParallel(),
                "mlp.down_proj": tp.RowwiseParallel(output_layouts=Shard(1)),
            },
        )

    tp.parallelize_module(
        model,
        mesh["tp"],
        {
            "model.norm": tp.SequenceParallel(),
            "lm_head": tp.ColwiseParallel(
                input_layouts=Shard(1),
                output_layouts=Shard(-1),  # for tp.loss_parallel
                use_local_output=False,  # for tp.loss_parallel
            ),
        },
    )

    for layer in model.model.layers:
        fully_shard(layer, mesh=mesh["dp"])
    fully_shard(model, mesh=mesh["dp"])

    LOGGER.info(f"Final Architecture: {model}")
    LOGGER.info(f"{sum(p.numel() for p in model.parameters())} model parameters")

    model = model.to_empty(device=device)
    model.init_weights()
    model.train()

    LOGGER.info(f"{get_mem_stats(device)}")

    # NOTE: since this can download data, make sure to do the main process first on each node
    # since we manually specified HF_HOME to be a node local drive.
    with rank_ordered(should_go_first=local_rank == 0):
        train_data = _load_and_preprocess_data(args, config)
    LOGGER.info(f"{len(train_data)} training samples")

    dataloader = DataLoader(
        train_data,
        batch_size=args.batch_size,
        collate_fn=default_data_collator,
        num_workers=1,
        prefetch_factor=2,
        # NOTE: this sampler will split dataset evenly across workers
        sampler=DistributedSampler(
            train_data,
            shuffle=True,
            drop_last=True,
            num_replicas=mesh["dp"].size(),  # equivalent to `num_nodes`
            rank=mesh["dp"].get_local_rank(),  # equivalent to `rank // num_nodes`
        ),
    )
    LOGGER.info(f"{len(dataloader)} batches per epoch")

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, fused=True)
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=1000, eta_min=args.lr * 1e-2
    )

    exp_dir: Path = Path(args.save_dir) / args.experiment_name

    # attempt resume
    state = {
        "epoch": 0,
        "global_step": 0,
        "epoch_step": 0,
        "running_loss": 0,
    }
    resumed = False
    if (exp_dir / "state.json").exists():
        DCP.load(
            dict(model=model, optimizer=optimizer),
            checkpoint_id=exp_dir / "checkpoint",
        )
        lr_scheduler.load_state_dict(
            torch.load(
                exp_dir / "lr_scheduler.pt", map_location=device, weights_only=True
            )
        )
        with open(exp_dir / "state.json") as fp:
            state = json.load(fp)
        resumed = True
    LOGGER.info(f"Resumed={resumed} | {state}")
    dist.barrier()

    if (exp_dir.is_mount() and rank == 0) or (
        not exp_dir.is_mount() and local_rank == 0
    ):
        LOGGER.info(f"Creating experiment root directory")
        exp_dir.mkdir(parents=True, exist_ok=True)
    dist.barrier()

    if rank == 0:
        wandb.init(
            project="distributed-training-guide",
            dir=exp_dir,
            name=args.experiment_name,
            id=args.experiment_name,
            resume="must" if resumed else None,
            save_code=True,
            config={
                "args": vars(args),
                "training_data_size": len(train_data),
                "num_batches": len(dataloader),
                "world_size": world_size,
            },
        )

    timers = {k: LocalTimer(device) for k in ["data", "forward", "backward", "update"]}

    for state["epoch"] in range(state["epoch"], args.num_epochs):
        LOGGER.info(f"Begin epoch {state['epoch']} at step {state['epoch_step']}")

        progress_bar = tqdm.tqdm(range(len(dataloader)), disable=True)
        if state["epoch_step"] > 0:
            progress_bar.update(state["epoch_step"])

        batches = iter(dataloader)

        for i_step in range(len(dataloader)):
            with timers["data"], torch.no_grad():
                batch = next(batches)
                batch = {k: v.to(device=device) for k, v in batch.items()}
                batch["position_ids"] = torch.arange(
                    0, args.seq_length, device=device, dtype=torch.long
                ).unsqueeze(0)

            if i_step < state["epoch_step"]:
                # NOTE: for resuming
                continue

            with tp.loss_parallel(), timers["forward"]:
                outputs = model(**batch)

            with tp.loss_parallel(), timers["backward"]:
                outputs.loss.backward()

            with timers["update"]:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad(set_to_none=True)

            state["global_step"] += 1
            state["epoch_step"] += 1
            state["running_loss"] += outputs.loss.item()
            progress_bar.update(1)

            if state["global_step"] % args.log_freq == 0:
                tok_per_step = mesh["dp"].size() * args.batch_size * args.seq_length
                ms_per_step = sum(t.avg_elapsed_ms() for t in timers.values())
                info = {
                    "global_step": state["global_step"],
                    "lr": lr_scheduler.get_last_lr()[0],
                    "running_loss": state["running_loss"] / args.log_freq,
                    "epoch": state["epoch"],
                    "epoch_progress": state["epoch_step"] / len(dataloader),
                    "num_batches_remaining": len(dataloader) - i_step,
                    "tok/s": 1000 * tok_per_step / ms_per_step,
                    **get_mem_stats(device),
                    "time/total": sum(t.avg_elapsed_ms() for t in timers.values()),
                    **{
                        f"time/{k}": timer.avg_elapsed_ms()
                        for k, timer in timers.items()
                    },
                }

                LOGGER.info(info)
                if rank == 0:
                    wandb.log(info, step=state["global_step"])

                torch.cuda.reset_peak_memory_stats(device)
                state["running_loss"] = 0
                for t in timers.values():
                    t.reset()

            if state["global_step"] % args.ckpt_freq == 0:
                LOGGER.info("Saving checkpoint.")
                dist.barrier()
                # NOTE: we have to call this on ALL ranks
                DCP.save(
                    dict(model=model, optimizer=optimizer),
                    checkpoint_id=exp_dir / "checkpoint",
                )
                if rank == 0:
                    torch.save(lr_scheduler.state_dict(), exp_dir / "lr_scheduler.pt")
                    with open(exp_dir / "state.json", "w") as fp:
                        json.dump(state, fp)
                dist.barrier()

        state["epoch_step"] = 0


def get_mem_stats(device=None):
    mem = torch.cuda.memory_stats(device)
    props = torch.cuda.get_device_properties(device)
    return {
        "total_gb": 1e-9 * props.total_memory,
        "curr_alloc_gb": 1e-9 * mem["allocated_bytes.all.current"],
        "peak_alloc_gb": 1e-9 * mem["allocated_bytes.all.peak"],
        "curr_resv_gb": 1e-9 * mem["reserved_bytes.all.current"],
        "peak_resv_gb": 1e-9 * mem["reserved_bytes.all.peak"],
    }


def _load_and_preprocess_data(args, config):
    """
    Function created using code found in
    https://github.com/huggingface/transformers/blob/v4.45.1/examples/pytorch/language-modeling/run_clm_no_trainer.py
    """
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)

    data = datasets.load_dataset(args.dataset_name, trust_remote_code=True)

    column_names = data["train"].column_names
    text_column_name = "text" if "text" in column_names else column_names[0]

    def tokenize_function(examples):
        return tokenizer(examples[text_column_name])

    tokenized_datasets = data.map(
        tokenize_function,
        batched=True,
        remove_columns=column_names,
        num_proc=multiprocessing.cpu_count(),
        load_from_cache_file=True,
        desc="Running tokenizer on dataset",
    )

    seq_length = args.seq_length or tokenizer.model_max_length
    if seq_length > config.max_position_embeddings:
        seq_length = min(1024, config.max_position_embeddings)

    # Main data processing function that will concatenate all texts from our dataset and generate chunks of block_size.
    def group_texts(examples):
        # Concatenate all texts.
        concatenated_examples = {k: list(chain(*examples[k])) for k in examples.keys()}
        total_length = len(concatenated_examples[list(examples.keys())[0]])
        # We drop the small remainder, and if the total_length < block_size  we exclude this batch and return an empty dict.
        # We could add padding if the model supported it instead of this drop, you can customize this part to your needs.
        if total_length > seq_length:
            total_length = (total_length // seq_length) * seq_length
        # Split by chunks of max_len.
        result = {
            k: [t[i : i + seq_length] for i in range(0, total_length, seq_length)]
            for k, t in concatenated_examples.items()
        }
        result["labels"] = result["input_ids"].copy()
        return result

    lm_datasets = tokenized_datasets.map(
        group_texts,
        batched=True,
        num_proc=multiprocessing.cpu_count(),
        load_from_cache_file=True,
        desc=f"Grouping texts in chunks of {seq_length}",
    )

    return lm_datasets["train"]


@contextmanager
def rank_ordered(*, should_go_first: bool):
    if should_go_first:
        yield
    dist.barrier()
    if not should_go_first:
        yield
    dist.barrier()


class LocalTimer:
    def __init__(self, device: torch.device):
        if device.type == "cpu":
            self.synchronize = lambda: torch.cpu.synchronize(device=device)
        elif device.type == "cuda":
            self.synchronize = lambda: torch.cuda.synchronize(device=device)
        self.measurements = []
        self.start_time = None

    def __enter__(self):
        self.synchronize()
        self.start_time = time.time()
        return self

    def __exit__(self, type, value, traceback):
        if traceback is None:
            self.synchronize()
            end_time = time.time()
            self.measurements.append(end_time - self.start_time)
        self.start_time = None

    def avg_elapsed_ms(self):
        return 1000 * (sum(self.measurements) / len(self.measurements))

    def reset(self):
        self.measurements = []
        self.start_time = None


def _get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("-e", "--experiment-name", default=None, required=True)
    parser.add_argument("-d", "--dataset-name", default=None, required=True)
    parser.add_argument("-m", "--model-name", default=None, required=True)
    parser.add_argument("--save-dir", default="../outputs")
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--num-epochs", default=100, type=int)
    parser.add_argument("--lr", default=3e-5, type=float)
    parser.add_argument("-b", "--batch-size", default=1, type=int)
    parser.add_argument("--log-freq", default=100, type=int)
    parser.add_argument("--ckpt-freq", default=500, type=int)
    parser.add_argument("-s", "--seq-length", default=None, type=int)
    parser.add_argument("--tp", default=8, type=int)
    return parser


if __name__ == "__main__":
    main()
