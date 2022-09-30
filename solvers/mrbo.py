from benchopt import BaseSolver
from benchopt.stopping_criterion import SufficientProgressCriterion

from benchopt import safe_import_context

with safe_import_context() as import_ctx:
    import numpy as np
    from numba import njit, int64, float64
    from numba.experimental import jitclass
    constants = import_ctx.import_from('constants')
    joint_shia = import_ctx.import_from('hessian_approximation', 'joint_hia')
    MinibatchSampler = import_ctx.import_from(
        'minibatch_sampler', 'MinibatchSampler'
    )
    LearningRateScheduler = import_ctx.import_from(
        'learning_rate_scheduler', 'LearningRateScheduler'
    )


class Solver(BaseSolver):
    """Single loop Bi-level optimization algorithm."""
    name = 'MRBO'

    stopping_criterion = SufficientProgressCriterion(
        patience=constants.PATIENCE, strategy='callback'
    )

    # any parameter defined here is accessible as a class attribute
    parameters = {
        'step_size': [.1],
        'outer_ratio': [1.],
        'n_hia_step': [10],
        'batch_size': [64],
        'eta': [.5],
        'eval_freq': [1],
    }

    @staticmethod
    def get_next(stop_val):
        return stop_val + 1

    def set_objective(self, f_train, f_test, inner_var0, outer_var0, numba):
        if numba:
            self.f_inner = f_train.numba_oracle
            self.f_outer = f_test.numba_oracle
            spec_minibatch_sampler = [
                ('n_samples', int64),
                ('batch_size', int64),
                ('i_batch', int64),
                ('n_batches', int64),
                ('batch_order', int64[:]),
            ]
            self.MinibatchSampler = jitclass(MinibatchSampler,
                                             spec_minibatch_sampler)

            spec_scheduler = [
                ('i_step', int64),
                ('constants', float64[:]),
                ('exponents', float64[:])
            ]
            self.LearningRateScheduler = jitclass(LearningRateScheduler,
                                                  spec_scheduler)

            def mrbo(variance_reduction):
                def f(*args, **kwargs):
                    return njit(_mrbo)(variance_reduction, *args, **kwargs)
                return f
            self.mrbo = mrbo(njit(joint_shia))
        else:
            self.f_inner = f_train
            self.f_outer = f_test

            def mrbo(variance_reduction):
                def f(*args, **kwargs):
                    return njit(_mrbo(variance_reduction, *args, **kwargs))
                return f
            self.mrbo = mrbo(joint_shia)
            self.MinibatchSampler = MinibatchSampler
            self.LearningRateScheduler = LearningRateScheduler
        self.inner_var0 = inner_var0
        self.outer_var0 = outer_var0
        self.numba = numba

    def run(self, callback):
        eval_freq = self.eval_freq
        rng = np.random.RandomState(constants.RANDOM_STATE)

        # Init variables
        inner_var = self.inner_var0.copy()
        outer_var = self.outer_var0.copy()
        memory_inner = np.zeros((2, *inner_var.shape), inner_var.dtype)
        memory_outer = np.zeros((2, *outer_var.shape), outer_var.dtype)

        # Init sampler and lr scheduler
        if self.batch_size == 'full':
            batch_size_inner = self.f_inner.n_samples
            batch_size_outer = self.f_outer.n_samples
        else:
            batch_size_inner = self.batch_size
            batch_size_outer = self.batch_size
        inner_sampler = self.MinibatchSampler(
            self.f_inner.n_samples, batch_size=batch_size_inner
        )
        outer_sampler = self.MinibatchSampler(
            self.f_outer.n_samples, batch_size=batch_size_outer
        )
        step_sizes = np.array(  # (inner_ss, hia_lr, eta, outer_ss)
            [
                self.step_size,
                self.step_size,
                self.eta,
                self.step_size / self.outer_ratio,
            ]
        )
        exponents = np.array([1/3, 0, 2/3, 1/3])
        lr_scheduler = self.LearningRateScheduler(
            np.array(step_sizes, dtype=float), exponents
        )

        # Start algorithm
        while callback((inner_var, outer_var)):
            inner_var, outer_var, memory_inner, memory_outer = self.mrbo(
                self.f_inner, self.f_outer,
                inner_var, outer_var, memory_inner, memory_outer,
                eval_freq, inner_sampler, outer_sampler,
                lr_scheduler, self.n_hia_step,
                seed=rng.randint(constants.MAX_SEED)
            )
        self.beta = (inner_var, outer_var)

    def get_result(self):
        return self.beta


def _mrbo(joint_shia, inner_oracle, outer_oracle, inner_var, outer_var,
          memory_inner, memory_outer, max_iter, inner_sampler, outer_sampler,
          lr_scheduler, n_hia_step, seed=None):

    # Set seed for randomness
    if seed is not None:
        np.random.seed(seed)

    for i in range(max_iter):
        inner_lr, hia_lr, eta, outer_lr = lr_scheduler.get_lr()

        # Step.1 - Update direction for z with momentum
        slice_inner, _ = inner_sampler.get_batch()
        grad_inner_var = inner_oracle.grad_inner_var(
            inner_var, outer_var, slice_inner
        )
        grad_inner_var_old = inner_oracle.grad_inner_var(
            memory_inner[0], memory_outer[0], slice_inner
        )
        memory_inner[1] = eta * grad_inner_var + (1-eta) * (
            memory_inner[1] + grad_inner_var - grad_inner_var_old
        )

        # Step.2 - Compute implicit grad approximation with HIA
        slice_outer, _ = outer_sampler.get_batch()
        grad_outer, impl_grad = outer_oracle.grad(
            inner_var, outer_var, slice_outer
        )
        grad_outer_old, impl_grad_old = outer_oracle.grad(
            memory_inner[0], memory_outer[0], slice_outer
        )
        ihvp, ihvp_old = joint_shia(
            inner_oracle, inner_var, outer_var, grad_outer,
            memory_inner[0], memory_outer[0], grad_outer_old,
            inner_sampler, n_hia_step, hia_lr
        )
        impl_grad -= inner_oracle.cross(
            inner_var, outer_var, ihvp, slice_inner
        )
        impl_grad_old -= inner_oracle.cross(
            memory_inner[0], memory_outer[0], ihvp_old, slice_inner
        )

        # Step.3 - Update direction for x with momentum
        memory_outer[1] = eta * impl_grad + (1-eta) * (
            memory_outer[1] + impl_grad - impl_grad_old
        )

        # Step.4 - Save the current variables
        memory_inner[0] = inner_var
        memory_outer[0] = outer_var

        # Step.5 - update the variables with the directions
        inner_var -= inner_lr * memory_inner[1]
        outer_var -= outer_lr * memory_outer[1]

        # Step.6 - project back to the constraint set
        inner_var, outer_var = inner_oracle.prox(inner_var, outer_var)
    return inner_var, outer_var, memory_inner, memory_outer
