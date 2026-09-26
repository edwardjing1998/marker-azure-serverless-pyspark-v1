class PipelineError(RuntimeError):
    """Safe, non-secret error that may be included in a run report."""

class GpuFatalError(PipelineError):
    pass

class CpuResourceLimit(PipelineError):
    """Only this class, not arbitrary exceptions, is eligible for GPU retry."""

class RunBudgetExceeded(PipelineError):
    pass
