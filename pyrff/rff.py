"""
Implementation of random fourier features (RFF).


"""
import numba
import numpy
import scipy.linalg
import typing


from . exceptions import ShapeError, DtypeError


def _compute_inverse(A:numpy.ndarray) -> numpy.ndarray:
    """Compute the inverse of a matrix.

    Uses `scipy.linalg.cho_factor`, but falls back to `scipy.linalg.inv` on errors.
    """
    try:
        A_cholesky = scipy.linalg.cho_factor(A)
        A_inverse = scipy.linalg.cho_solve(A_cholesky, numpy.eye(A.shape[0]))
        return A_inverse
    except scipy.linalg.LinAlgError:
        A_inverse = scipy.linalg.inv(A)
        return A_inverse


def _vectorize(fun):
    """
    Decorates a function requiring 2D inputs such that 1D inputs are automatically
    expanded but the dimensionality of the return value is decremented again.
    """
    def wrapper(x):
        x2d = numpy.atleast_2d(x)
        if numpy.ndim(x) == 1:
            return fun(x2d)[0]
        else:
            return fun(x2d)
    return wrapper


def sample_rff(
    lengthscales:numpy.ndarray,
    scaling:float,
    noise:float,
    kernel_nu:float,
    X:numpy.ndarray,
    Y:numpy.ndarray,
    M:int,
) -> typing.Tuple[
    typing.Callable[[numpy.ndarray], typing.Union[numpy.ndarray, float]],
    typing.Callable[[numpy.ndarray], typing.Union[numpy.ndarray, float]],
    typing.Callable[[numpy.ndarray], numpy.ndarray],
]:
    """
    Samples an approximate function from a GP with given kernel function and zero mean.

    It returns not only the approximate function, but also a njit-decorated version and its njit-decorated gradient.
    The njit-decorated callables have a compilation overhead on the first call!
    After that they are about 3.5x and 6x faster than non-njitted alternatives.

    Adapted from other implementations:
    + [Hernández-Lobato, 2014](https://bitbucket.org/jmh233/codepesnips2014/src/master/sourceFiles/sampleMinimum.m)
    + [Cornell-MOE](https://github.com/wujian16/Cornell-MOE/blob/master/pes/PES/sample_minimum.py)
    + [Bradford, 2018](https://github.com/Eric-Bradford/TS-EMO/blob/master/TSEMO_V3.m#L495)

    Parameters
    ----------
    lengthscales : numpy.ndarray
        lengthscale values for each dimension (D,). It corresponds to...
            '1/l²' from [Hernández-Lobato, 2014] implementation
            'l' from Cornell-MOE implementation
            'ell' from [Bradford, 2018] implementation, line 501
    scaling : float
        the kernel standard deviation sigma. (As in k(x,x') = scaling**2 * ExpQuad()) It corresponds to...
            'sqrt(sigma)' from [Hernández-Lobato, 2014] implementation
            'sqrt(alpha)' from Cornell-MOE implementation
            'sqrt(sf2)' from [Bradford, 2018] implementation, line 502
    noise : float
        the observation noise from the likelihood function. It corresponds to...
            'sigma0' from [Hernández-Lobato, 2014] implementation
            'noise' from Cornell-MOE implementation
            'sn2' from [Bradford, 2018] implementation, line 503
    kernel_nu : float
        degree of freedom of the GP kernel, for example
            `numpy.inf` for squared exponential / RBF / exponential quadratic
            1/2 for Matern12
            3/2 for Matern32
    X : numpy.ndarray
        coordinates of observations (?, D)
    Y : numpy.ndarray
        observed values at coordinates (?,)
    M : int
        number of fourier features to use in the approximation
        according to [Mutny & Krause, 2018], the approximation error scales O(m^(1/2))

    Returns
    -------
    f_rff : callable
        may be called with (?, D) or (D,) coordinates, returning (?,) or float respectively
    f_rff_njit : callable
        jit-compiled version of the above
    g_rff_njit : callable
        jit-compiled gradient, takes (?, D) or (D,) coordinates, returning (?, D) or (?, D) respectively
    """
    # check inputs
    X = numpy.atleast_2d(X)
    Y = numpy.atleast_1d(Y)
    D = X.shape[1]
    N_obs = len(Y)
    lengthscales = numpy.atleast_1d(lengthscales)
    if not X.shape == (N_obs, D):
        raise ShapeError('Shapes of X and Y do not match.', expected='(?, D), (?,)', actual=f'{X.shape}, {Y.shape}')
    if not lengthscales.shape == (D,):
        raise ShapeError('Lengthscales and data dimensions do not match.', lengthscales.shape, (D,))
    if not numpy.ndim(scaling) == 0:
        raise ShapeError('Argument "scaling" must be a scalar.')
    if not numpy.ndim(noise) == 0:
        raise ShapeError('Argument "noise" must be a scalar.')
    if not numpy.isscalar(kernel_nu) and kernel_nu > 0:
        raise DtypeError('Argument "kernel_nu" must be a positive-definite scalar.')

    # construct function to sample p(w) (see [Bradford, 2018], equations 27 and 28)
    if numpy.isinf(kernel_nu):
        def p_w(size:tuple) -> numpy.ndarray:
            return numpy.random.normal(loc=0, scale=1 / lengthscales, size=size)
    else:
        def p_w(size:tuple) -> numpy.ndarray:
            return scipy.stats.t.rvs(loc=0, scale=1 / lengthscales, df=kernel_nu, size=size)

    W = p_w(size=(M, D))
    assert W.shape == (M, D)
    B = numpy.random.uniform(0, 2*numpy.pi, size=(M, 1))
    assert B.shape == (M, 1)
    # see Bradford 2018, equation 26
    alpha = scaling**2
    # just to avoid re-computing it all the time:
    sqrt_2_alpha_over_m = numpy.sqrt(2*alpha/M)

    zeta = sqrt_2_alpha_over_m * numpy.cos(numpy.dot(W, X.T) + B)
    assert zeta.shape == (M, N_obs)

    A = numpy.divide(numpy.dot(zeta, zeta.T), noise) + numpy.eye(M)
    A_inverse = _compute_inverse(A)
    assert A_inverse.shape == (M, M)
    mean_of_post_theta = numpy.divide(numpy.dot(numpy.dot(A_inverse, zeta), Y), noise)
    assert mean_of_post_theta.shape == (M,)
    variance_of_post_theta = A_inverse
    assert variance_of_post_theta.shape == (M, M)

    # sample function features
    sample_of_theta = numpy.random.multivariate_normal(mean_of_post_theta, variance_of_post_theta)
    assert sample_of_theta.shape == (M,)

    def rff_approximation(x:numpy.ndarray) -> numpy.ndarray:
        """Implements the RFF approximation function.

        Parameters
        ----------
        x : numpy.ndarray
            a 2D array of coordinates (?, D)

        Returns
        -------
        approx_y : numpy.ndarray
            function evaluations (?,)
        """
        N = x.shape[0]
        assert x.shape == (N, D)
        phi_x = sqrt_2_alpha_over_m*numpy.cos(numpy.dot(W, x.T) + B)
        assert phi_x.shape == (M, N)
        approx_y = numpy.dot(phi_x.T, sample_of_theta)
        assert approx_y.shape == (N,)
        return approx_y

    def rff_approximation_gradient(x:numpy.ndarray) -> numpy.ndarray:
        """Implements the gradient of the RFF approximation function.

        Parameters
        ----------
        x : numpy.ndarray
            a 2D array of coordinates (?, D)

        Returns
        -------
        dydx : numpy.ndarray
            evaluations of the gradient w.r.t. x (?, D)
        """
        N = x.shape[0]
        assert x.shape == (N, D)
        temp = numpy.sin(numpy.dot(W, x.T) + B)
        assert temp.shape == (M, N)
        # the following is a refactor of the Cornell-MOE implementation that supports numba.njit (about 6x faster)
        gradient = numpy.empty((N, D))
        for n in range(N):
            gradient_of_phi_x = -sqrt_2_alpha_over_m * temp[:,n] * W.T
            gradient[n] = numpy.dot(gradient_of_phi_x, sample_of_theta)
        return gradient

    # call the function with test data to make sure that it works
    X_test = numpy.array([X[0], X[0]])
    assert numpy.shape(rff_approximation(X_test)) == (2,)
    assert numpy.shape(rff_approximation_gradient(X_test)) == (2, D)

    # use the _vectorize decorator to make the functions compatible with (D,) and (?, D) inputs
    f_rff = _vectorize(rff_approximation)
    f_rff_njit = _vectorize(numba.njit(rff_approximation))
    g_rff_njit = _vectorize(numba.njit(rff_approximation_gradient))

    return f_rff, f_rff_njit, g_rff_njit
