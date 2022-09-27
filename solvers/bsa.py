
from benchopt import BaseSolver
from benchopt.stopping_criterion import SufficientProgressCriterion

from benchopt import safe_import_context

with safe_import_context() as import_ctx:
    import numpy as np
    from numba import njit, int64, float64
    from numba.experimental import jitclass
    constants = import_ctx.import_from('constants')
    hia = import_ctx.import_from('hessian_approximation', 'hia')
    sgd_inner = import_ctx.import_from('sgd_inner', 'sgd_inner')
    MinibatchSampler = import_ctx.import_from(
        'minibatch_sampler', 'MinibatchSampler'
    )
    LearningRateScheduler = import_ctx.import_from(
        'learning_rate_scheduler', 'LearningRateScheduler'
    )


class Solver(BaseSolver):
    """BSA - Two loops solver."""
    name = 'BSA'

    stopping_criterion = SufficientProgressCriterion(
        patience=constants.PATIENCE, strategy='callback'
    )

    # any parameter defined here is accessible as a class attribute
    parameters = {
        'step_size': constants.STEP_SIZES,
        'outer_ratio': constants.OUTER_RATIOS,
        'n_inner_step': constants.N_INNER_STEPS,
        'n_hia_step': constants.N_HIA_STEPS,
        'batch_size': constants.BATCH_SIZES,
    }

    @staticmethod
    def get_next(stop_val):
        return stop_val + 1

    def set_objective(self, f_train, f_test, inner_var0, outer_var0, numba):
        if numba:
            self.f_inner = f_train.numba_oracle
            self.f_outer = f_test.numba_oracle
            self.sgd_inner = njit(sgd_inner)
            self.hia = njit(hia)

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
            self.bsa = njit(bsa(self.sgd_inner, self.hia))
        else:
            self.f_inner = f_train
            self.f_outer = f_test
            self.sgd_inner = sgd_inner
            self.hia = hia
            self.MinibatchSampler = MinibatchSampler
            self.LearningRateScheduler = LearningRateScheduler
            self.bsa = bsa(self.sgd_inner, self.hia)

        self.inner_var0 = inner_var0
        self.outer_var0 = outer_var0
        self.numba = numba

    def run(self, callback):
        eval_freq = constants.EVAL_FREQ
        rng = np.random.RandomState(constants.RANDOM_STATE)

        # Init variables
        outer_var = self.outer_var0.copy()
        inner_var = self.inner_var0.copy()

        # Init sampler and lr
        inner_sampler = self.MinibatchSampler(
            self.f_inner.n_samples, self.inner_batch_size
        )
        outer_sampler = self.MinibatchSampler(
            self.f_outer.n_samples, self.outer_batch_size
        )
        step_sizes = np.array(
            [self.step_size, self.step_size, self.step_size / self.outer_ratio]
        )
        exponents = np.array([.5, 0., .5])
        lr_scheduler = self.LearningRateScheduler(
            np.array(step_sizes, dtype=float), exponents
        )

        # Start algorithm
        callback((inner_var, outer_var))
        inner_var = sgd_inner(
            self.f_inner, inner_var, outer_var,
            step_size=self.step_size, inner_sampler=inner_sampler,
            n_inner_step=self.n_inner_step
        )
        while callback((inner_var, outer_var)):
            inner_var, outer_var = bsa(
                self.f_inner, self.f_outer,
                inner_var, outer_var, eval_freq, lr_scheduler,
                self.n_inner_step, n_hia_step=self.n_hia_step,
                inner_sampler=inner_sampler, outer_sampler=outer_sampler,
                seed=rng.randint(constants.MAX_SEED)
            )

        self.beta = (inner_var, outer_var)

    def get_result(self):
        return self.beta


def _bsa(sgd_inner, hia, inner_oracle, outer_oracle, inner_var, outer_var,
         max_iter, lr_scheduler, n_inner_step, n_hia_step,
         inner_sampler, outer_sampler, seed=None):
    """Numba compatible BSA algorithm.

    Parameters
    ----------
    inner_oracle, outer_oracle: NumbaOracle
        Inner and outer problem oracles used to compute gradients, etc...
    inner_var, outer_var: ndarray
        Current estimates of the inner and outer variables of the bi-level
        problem.
    max_iter: int
        Maximal number of iteration for the outer problem.
    outer_step_size: float
        Step size to update the outer variable.
    n_inner_step: int
        Maximal number of iteration for the inner problem.
    inner_step_size: float
        Step size to update the inner variable.
    n_hia_step: int
        Maximal number of iteration for the HIA problem.
    hia_step_size: float
        Step size for the HIA sub-routine.
    inner_sampler, outer_sampler: MinibatchSampler
        Sampler to get minibatch in a fast and efficient way for the inner and
        outer problems.
    """

    # Set seed for randomness
    if seed is not None:
        np.random.seed(seed)

    for i in range(max_iter):
        inner_lr, hia_lr, outer_lr = lr_scheduler.get_lr()

        outer_slice, _ = outer_sampler.get_batch()
        grad_in, grad_out = outer_oracle.grad(
            inner_var, outer_var, outer_slice
        )

        implicit_grad = hia(
            inner_oracle, inner_var, outer_var, grad_in,
            inner_sampler, n_hia_step, hia_lr
        )
        inner_slice, _ = inner_sampler.get_batch()
        implicit_grad = inner_oracle.cross(
            inner_var, outer_var, implicit_grad, inner_slice
        )
        grad_outer_var = grad_out - implicit_grad

        outer_var -= outer_lr * grad_outer_var
        inner_var, outer_var = inner_oracle.prox(inner_var, outer_var)

        inner_var = sgd_inner(
            inner_oracle, inner_var, outer_var, step_size=inner_lr,
            inner_sampler=inner_sampler, n_inner_step=n_inner_step
        )
    return inner_var, outer_var


def bsa(sgd_inner, hia):
    def f(*args, **kwargs):
        return _bsa(sgd_inner, hia, *args, **kwargs)
    return f
