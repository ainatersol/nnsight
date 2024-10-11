
from vllm.executor.gpu_executor import GPUExecutor


class NNsightGPUExecutor(GPUExecutor):


    def _get_worker_module_and_class(self):
        return ("nnsight.models.vllm.workers.GPUWorker", "NNsightGPUWorker", None)
