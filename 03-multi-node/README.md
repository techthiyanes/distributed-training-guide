# Multi GPU across multiple nodes

**NOTE: This chapter's code builds off of [chapter 2](../02-multi-gpu).**

Run this command on **every** participating node. See [chapter 4](../04-job-launchers) for how to do this automatically (there are many ways).

```bash
cd distributed-training-guide/03-multi-node
export HF_HOME=../.cache
export TORCHELASTIC_ERROR_FILE=../error.json
export OMP_NUM_THREADS=1
torchrun \
    --rdzv-id multi-node \
    --rdzv-backend c10d \
    --rdzv-endpoint <IP ADDRESS of main node>:<port> \
    --nnodes 2 \
    --nproc-per-node gpu \
    --redirects 3 \
    --log-dir ../logs \
    train_llm.py \
    --experiment-name gpt2-alpaca-multi-node-$(date +%Y-%m-%dT%H-%M-%S) \
    --dataset-name tatsu-lab/alpaca \
    --model-name openai-community/gpt2
```

Assumes:
1. You are using the same enviroment on both machines
2. You are logged into wandb on both machines

Quick Jump
- [How multi node works](#how-multi-node-works)
- [Managing python venvs across nodes](#managing-your-python-virtual-environment-across-nodes)
- [Managing dataset/model across nodes](#mangaging-your-datasetmodel-checkpoints-across-nodes)
- Code Changes
    - [Using local rank for device instead of rank](#using-local-rank-for-device-instead-of-rank)
    - [Using local rank for DDP instead of rank](#using-local-rank-for-ddp-instead-of-rank)
    - [Checking whether exp_dir is a shared network drive](#checking-whether-exp_dir-is-a-shared-network-drive)
    - [Downloaded Model/Dataset directory](#downloaded-modeldataset-directory)

## How multi node works

It actually works in much the same way as the multi GPU. Since in the single node setting we have multiple processes, now we are just adding extra processes on different machines.

The main differences here to consider are:
1. How to maintain the same environment on every node
2. How the nodes get in contact with each other (the `rdzv` arguments in the torchrun command)
3. Your code needs to use `local_rank` instead of `rank` some places. `rank` is between 0 and world_size, so if you have 2 machines, the second machine may have ranks 8-16. Local rank on the second machine will still be 0-8.
4. How each node will access the data

Error reporting/handling becomes extremely important with more than 1 node. Networking issues are very common, and there are some subtle things that you need to ensure are identical between the machines.

## Managing your python virtual environment across nodes

For this the easiest approach is to create your python virtual environment in a shared network drive that all nodes can access. This way all of your nodes are using the exact same python executable/environment.

Creating the virtual environment is the same as normal, you just want the directory to be shared.

## Mangaging your dataset/model checkpoints across nodes

Again, the easiest approach here is to keep your data in a shared network drive. One thing to note is that shared network drives are slower to read from than node local drives. If you run into slowdowns in data loading, you can copy the data or model into node local storage.

When using `transformers` or `datasets`, make sure to set the `$HF_HOME` environment variable to control where huggingface downloads both datasets and model weights.

## Code Changes

Not much has to change code wise. The main thing is how your data is stored/organized.

### Using local rank for device instead of rank

```diff
 rank = dist.get_rank()
+local_rank = rank % torch.cuda.device_count()
-device = torch.device(f"cuda:{rank}")
+device = torch.device(f"cuda:{local_rank}")
```

### Using local rank for DDP instead of rank

```diff
-model = DistributedDataParallel(model, device_ids=[rank], output_device=rank)
+model = DistributedDataParallel(
+    model, device_ids=[local_rank], output_device=local_rank
+)
```

### Checking whether exp_dir is a shared network drive

If exp_dir is a node local drive, then we need each node to create the directory separately (on the process with `local_rank==0`). We can use `os.path.ismount` or `Path.is_mount` to check whether it is a shared network drive. Otherwise if it's a share drive we can create it in the process with `rank==0` like normal.

```diff
-if rank == 0:
+if (exp_dir.is_mount() and rank == 0) or (
+    not exp_dir.is_mount() and local_rank == 0
+):
     exp_dir.mkdir(parents=True, exist_ok=True) 
```

### Downloaded Model/Dataset directory

Huggingface `transformers` and `datasets` library will download things to `$HF_HOME` by default. `$HF_HOME` defaults to a **node local** value. There are two options for you here:

1. Keep `$HF_HOME` as node local and change `with rank0_first()` to be `with local_rank0_first()`
2. Change `$HF_HOME` to be a shared network drive

A third option which requires code changes to the code in this repo would be to do this automatically in code:

```python
@contextmanager
def rank_ordered(first: bool):
    if first:
        yield
    dist.barrier()
    if not first:
        yield
    dist.barrier()

# Determine if HF_HOME is node local or shared directory
hf_home_is_networked = os.path.ismount(os.environ["HF_HOME"])

if hf_home_is_networked:
    # We want rank 0 to go first (download will ONLY occur in rank 0) if directory is shared
    should_go_first = rank == 0
else:
    # If directory is node local we want a SINGLE process on the node to download the data (local rank 0)
    should_go_first = local_rank == 0

with rank_ordered(should_go_first):
    train_data = _load_and_preprocess_data(args, tokenizer, config)
```
