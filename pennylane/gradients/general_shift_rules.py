# Copyright 2018-2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Contains a function for generating generalized parameter shift rules."""
import functools
import itertools
import warnings

import numpy as np
import pennylane as qml


def process_shifts(rule, tol=1e-10, check_duplicates=True):
    """Utility function to process gradient rules.

    Args:
        rule (array): a ``(N, M)`` array corresponding to ``M`` terms
            with parameter shifts. ``N`` has to be either ``2`` or ``3``.
            The first row corresponds to the linear combination coefficients;
            the last row contains the shift values.
            If ``N=3``, the middle row contains the multipliers.
        tol (float): floating point tolerance used when comparing shifts/coefficients
            Terms with coefficients below ``tol`` will be removed.
        check_duplicates (bool): whether to check the input ``rule`` for duplicate
            shift values in its second row.

    This utility function accepts coefficients and shift values as well as optionally
    multipliers, and performs the following processing:

    - Set all small (within absolute tolerance ``tol``) coefficients and shifts to 0

    - Remove terms where the coefficients are 0 (including the ones set to 0 in the previous step)

    - Terms with the same shift value (and multiplier) are combined into a single term.

    - Finally, the terms are sorted according to the absolute value of ``shift``,
      This ensures that a zero-shift term, if it exists, is returned first.
    """
    # remove all small coefficients and shifts
    rule[np.abs(rule) < tol] = 0

    # remove columns where the coefficients are 0
    rule = rule[:, ~(rule[0] == 0)]

    if check_duplicates:
        round_decimals = int(-np.log10(tol))
        rounded_rule = np.round(rule[1:], round_decimals)
        # determine unique shifts or (multiplier, shift) combinations
        unique_mods = np.unique(rounded_rule, axis=1)

        if rule.shape[-1] != unique_mods.shape[-1]:
            if rule.shape[0] == 3:
                # sum columns that have the same multiplier and shift value
                # TODO: Check whether the list comprehension can be absorbed into a smart
                # single NumPy call
                coeffs = [
                    np.sum(rule[0, np.all(rounded_rule[1:] == mod.reshape((2, 1)), axis=0)])
                    for mod in unique_mods.T
                ]
            else:
                # sum columns that have the same shift value
                rounded_rule = rounded_rule[0]
                # TODO: Check whether the list comprehension can be absorbed into a smart
                # single NumPy call
                coeffs = [np.sum(rule[0, rounded_rule == s]) for s in unique_mods[0]]

            rule = np.vstack([np.stack(coeffs), unique_mods])

    # sort columns according to abs(shift)
    return rule[:, np.argsort(np.abs(rule)[-1])]


@functools.lru_cache(maxsize=None)
def eigvals_to_frequencies(eigvals):
    r"""Convert an eigenvalue spectrum to frequency values, defined
    as the the set of positive, unique differences of the eigenvalues in the spectrum.

    Args:
        eigvals (tuple[int, float]): eigenvalue spectra

    Returns:
        tuple[int, float]: frequencies

    **Example**

    >>> eigvals = (-0.5, 0, 0, 0.5)
    >>> eigvals_to_frequencies(eigvals)
    (0.5, 1.0)
    """
    unique_eigvals = sorted(set(eigvals))
    return tuple({j - i for i, j in itertools.combinations(unique_eigvals, 2)})


@functools.lru_cache(maxsize=None)
def frequencies_to_period(frequencies, decimals=5):
    r"""Returns the period of a Fourier series as defined
    by a set of frequencies.

    The period is simply :math:`2\pi/gcd(frequencies)`,
    where :math:`\text{gcd}` is the greatest common divisor.

    Args:
        spectra (tuple[int, float]): frequency spectra
        decimals (int): Number of decimal places to round to
            if there are non-integral frequencies.

    Returns:
        tuple[int, float]: frequencies

    **Example**

    >>> frequencies = (0.5, 1.0)
    >>> frequencies_to_period(frequencies)
    12.566370614359172
    """
    try:
        gcd = np.gcd.reduce(frequencies)

    except TypeError:
        # np.gcd only support integer frequencies
        exponent = 10**decimals
        frequencies = np.round(frequencies, decimals) * exponent
        gcd = np.gcd.reduce(np.int64(frequencies)) / exponent

    return 2 * np.pi / gcd


@functools.lru_cache(maxsize=None)
def _get_shift_rule(frequencies, shifts=None):
    n_freqs = len(frequencies)
    frequencies = qml.math.sort(qml.math.stack(frequencies))
    freq_min = frequencies[0]

    if len(set(frequencies)) != n_freqs or freq_min <= 0:
        raise ValueError(
            f"Expected frequencies to be a list of unique positive values, instead got {frequencies}."
        )

    mu = np.arange(1, n_freqs + 1)

    if shifts is None:  # assume equidistant shifts
        shifts = (2 * mu - 1) * np.pi / (2 * n_freqs * freq_min)
        equ_shifts = True
    else:
        shifts = qml.math.sort(qml.math.stack(shifts))
        if len(shifts) != n_freqs:
            raise ValueError(
                f"Expected number of shifts to equal the number of frequencies ({n_freqs}), instead got {shifts}."
            )
        if len(set(shifts)) != n_freqs:
            raise ValueError(f"Shift values must be unique, instead got {shifts}")

        equ_shifts = all(np.isclose(shifts, (2 * mu - 1) * np.pi / (2 * n_freqs * freq_min)))

    if len(set(np.round(np.diff(frequencies), 10))) <= 1 and equ_shifts:  # equidistant case
        coeffs = (
            freq_min
            * (-1) ** (mu - 1)
            / (4 * n_freqs * np.sin(np.pi * (2 * mu - 1) / (4 * n_freqs)) ** 2)
        )

    else:  # non-equidistant case
        sin_matr = -4 * np.sin(np.outer(shifts, frequencies))
        det_sin_matr = np.linalg.det(sin_matr)
        if abs(det_sin_matr) < 1e-6:
            warnings.warn(
                f"Solving linear problem with near zero determinant ({det_sin_matr}) "
                "may give unstable results for the parameter shift rules."
            )

        coeffs = -2 * np.linalg.solve(sin_matr.T, frequencies)

    coeffs = np.concatenate((coeffs, -coeffs))
    shifts = np.concatenate((shifts, -shifts))  # pylint: disable=invalid-unary-operand-type
    return np.stack([coeffs, shifts])


def _iterate_shift_rule_with_multipliers(rule, order, period):
    r"""Helper method to repeat a shift rule that includes multipliers multiple
    times along the same parameter axis for higher-order derivatives."""
    combined_rules = []

    # TODO: Optimization: Only iterate over half of the combinations and
    # double their coefficients (does this work with multipliers?)
    for partial_rule in itertools.product(rule, repeat=order):
        c, m, s = np.stack(partial_rule).T
        cumul_coeff = np.prod(c)
        cumul_shift = 0.0
        cumul_mult = np.prod(m)
        for _m, _s in zip(m, s):
            cumul_shift *= _m
            cumul_shift += _s
        if period is not None:
            cumul_shift = np.mod(cumul_shift + 0.5 * period, period) - 0.5 * period
        combined_rules.append(np.stack([cumul_coeff, cumul_mult, cumul_shift]))

    # combine all terms in the linear combination into a single
    # array, with coefficients on the first column and shifts on the second column.
    return qml.math.stack(combined_rules)


def _iterate_shift_rule(rule, order, period=None):
    r"""Helper method to repeat a shift rule multiple times along the same
    parameter axis for higher-order derivatives."""
    if len(rule[0]) == 3:
        return _iterate_shift_rule_with_multipliers(rule, order, period)

    combined_rules = []

    # TODO: Optimization: Only iterate over half of the combinations and
    # double their coefficients
    for partial_rule in itertools.product(rule, repeat=order):
        c, s = np.stack(partial_rule).T
        cumul_shift = sum(s)
        if period is not None:
            cumul_shift = np.mod(cumul_shift + 0.5 * period, period) - 0.5 * period
        combined_rules.append(np.stack([np.prod(c), cumul_shift]))

    # combine all terms in the linear combination into a single
    # array, with coefficients on the first column and shifts on the second column.
    return qml.math.stack(combined_rules)


def _combine_shift_rules_with_multipliers(rules):
    r"""Helper method to combine shift rules for multiple parameters that
    contain multipliers into simultaneous multivariate shift rules."""
    combined_rules = []

    for partial_rules in itertools.product(*rules):
        c, m, s = np.stack(partial_rules).T
        combined = np.concatenate([[np.prod(c)], m, s])
        combined_rules.append(np.stack(combined))

    return np.stack(combined_rules).T


def _combine_shift_rules(rules):
    r"""Helper method to combine shift rules for multiple parameters into
    simultaneous multivariate shift rules."""
    combined_rules = []

    for partial_rules in itertools.product(*rules):
        c, s = np.stack(partial_rules).T
        combined = np.concatenate([[np.prod(c)], s])
        combined_rules.append(np.stack(combined))

    return np.stack(combined_rules).T


@functools.lru_cache()
def generate_shift_rule(frequencies, shifts=None, order=1):
    r"""Computes the parameter shift rule for a unitary based on its generator's eigenvalue
    frequency spectrum.

    To compute gradients of circuit parameters in variational quantum algorithms, expressions for
    cost function first derivatives with respect to the variational parameters can be cast into
    linear combinations of expectation values at shifted parameter values. The coefficients and
    shifts defining the linear combination can be obtained from the unitary generator's eigenvalue
    frequency spectrum. Details can be found in
    `Wierichs et al. (2022) <https://doi.org/10.22331/q-2022-03-30-677>`__.

    Args:
        frequencies (tuple[int or float]): The tuple of eigenvalue frequencies. Eigenvalue
            frequencies are defined as the unique positive differences obtained from a set of
            eigenvalues.
        shifts (tuple[int or float]): the tuple of shift values. If unspecified,
            equidistant shifts are assumed. If supplied, the length of this tuple should match the
            number of given frequencies.
        order (int): the order of differentiation to compute the shift rule for

    Returns:
        tuple: a tuple of coefficients and shifts describing the gradient rule for the
        parameter-shift method. For parameter :math:`\phi`, the coefficients :math:`c_i` and the
        shifts :math:`s_i` combine to give a gradient rule of the following form:

        .. math:: \frac{\partial}{\partial\phi}f = \sum_{i} c_i f(\phi + s_i).

        where :math:`f(\phi) = \langle 0|U(\phi)^\dagger \hat{O} U(\phi)|0\rangle`
        for some observable :math:`\hat{O}` and the unitary :math:`U(\phi)=e^{iH\phi}`.

    Raises:
        ValueError: if ``frequencies`` is not a list of unique positive values, or if ``shifts``
            (if specified) is not a list of unique values the same length as ``frequencies``.

    **Examples**

    An example of obtaining the frequencies from generator eigenvalues, and obtaining the parameter
    shift rule:

    >>> eigvals = (-0.5, 0, 0, 0.5)
    >>> frequencies = eigvals_to_frequencies(eigvals)
    >>> generate_shift_rule(frequencies)
    [[ 0.4267767  -0.4267767  -0.0732233   0.0732233 ]
     [ 1.57079633 -1.57079633  4.71238898 -4.71238898]]

    An example with explicitly specified shift values:

    >>> frequencies = (1, 2, 4)
    >>> shifts = (np.pi / 3, 2 * np.pi / 3, np.pi / 4)
    >>> generate_shift_rule(frequencies, shifts)
    [[ 3.        , -3.        , -2.09077028,  2.09077028, 0.2186308, -0.2186308 ],
     [ 0.78539816, -0.78539816,  1.04719755, -1.04719755, 2.0943951, -2.0943951 ]]

    Higher order shift rules (corresponding to the :math:`n`-th derivative of the parameter) can be
    requested via the ``order`` argument. For example, to extract the second order shift rule for a
    gate with generator :math:`X/2`:

    >>> eigvals = (0.5, -0.5) frequencies = eigvals_to_frequencies(eigvals)
    >>> generate_shift_rule(frequencies, order=2)
    [[-0.5       ,   0.5       ],
     [ 0.        ,  -3.14159265]]

    This corresponds to :math:`\frac{\partial^2 f}{\partial phi^2} = \frac{1}{2} \left[f(\phi) -
    f(\phi-\pi)\right]`.
    """
    frequencies = tuple(f for f in frequencies if f > 0)
    rule = _get_shift_rule(frequencies, shifts=shifts)

    if order > 1:
        T = frequencies_to_period(frequencies)
        rule = _iterate_shift_rule(rule.T, order, period=T).T

    return process_shifts(rule, tol=1e-10)


def generate_multi_shift_rule(frequencies, shifts=None, orders=None):
    r"""Computes the parameter shift rule with respect to two parametrized unitaries,
    given their generator's eigenvalue frequency spectrum. This corresponds to a
    shift rule that computes off-diagonal elements of higher order derivative tensors.
    For the second order, this corresponds to the Hessian.

    Args:
        frequencies (list[tuple[int or float]]): List of eigenvalue frequencies corresponding
            to the each parametrized unitary.
        shifts (list[tuple[int or float]]): List of shift values corresponding to each parametrized
            unitary. If unspecified, equidistant shifts are assumed. If supplied, the length
            of each tuple in the list must be the same as the length of the corresponding tuple in
            ``frequencies``.
        orders (list[int]): the order of differentiation for each parametrized unitary.
            If unspecified, the first order derivative shift rule is computed for each parametrized
            unitary.

    Returns:
        tuple: a tuple of coefficients, shifts for the first parameter, and shifts for the
        second parameter, describing the gradient rule
        for the parameter-shift method.

        For parameters :math:`\phi_a` and :math:`\phi_b`, the
        coefficients :math:`c_i` and the shifts :math:`s^{(a)}_i`, :math:`s^{(b)}_i`,
        combine to give a gradient rule of the following form:

        .. math::

            \frac{\partial^2}{\partial\phi_a \partial\phi_b}f
            = \sum_{i} c_i f(\phi_a + s^{(a)}_i, \phi_b + s^{(b)}_i).

        where :math:`f(\phi_a, \phi_b) = \langle 0|U(\phi_a)^\dagger V(\phi_b)^\dagger \hat{O} V(\phi_b) U(\phi_a)|0\rangle`
        for some observable :math:`\hat{O}` and unitaries :math:`U(\phi_a)=e^{iH_a\phi_a}` and :math:`V(\phi_b)=e^{iH_b\phi_b}`.

    **Example**

    >>> generate_multi_shift_rule([(1,), (1,)])
    [[ 0.25      , -0.25      , -0.25      ,  0.25      ],
     [ 1.57079633,  1.57079633, -1.57079633, -1.57079633],
     [ 1.57079633, -1.57079633,  1.57079633, -1.57079633]])

    This corresponds to the gradient rule

    .. math::

        \frac{\partial^2 f}{\partial x\partial y} &= \frac{1}{4}
        \left[f(x+\pi/2, y+\pi/2) - f(x+\pi/2, y-\pi/2)\\
        &~~~- f(x-\pi/2, y+\pi/2) + f(x-\pi/2, y-\pi/2) \right].
    """
    rules = []
    shifts = shifts or [None] * len(frequencies)
    orders = orders or [1] * len(frequencies)

    for f, s, o in zip(frequencies, shifts, orders):
        rule = generate_shift_rule(f, shifts=s, order=o)
        rules.append(process_shifts(rule).T)

    return _combine_shift_rules(rules)


def generate_shifted_tapes(tape, idx, shifts, multipliers=None):
    r"""Generate a list of tapes where a marked trainable parameter
    has been shifted by the values given.

    Args:
        tape (.QuantumTape): input quantum tape
        idx (int): index of the trainable parameter to shift
        shifts (Sequence[float or int]): sequence of shift values
        multipliers (Sequence[float or int]): Sequence of multiplier values to
            scale the parameter by before shifting. If provided has to have the
            same length as ``shifts``. If not provided, the parameter will not
            be scaled.

    Returns:
        list[QuantumTape]: List of quantum tapes. Each tape has parameter
        ``idx`` shifted by consecutive values of ``shift``. The length
        of the returned list of tapes will match the length of ``shifts``.
    """
    params = list(tape.get_parameters())
    tapes = []

    for i, s in enumerate(shifts):
        new_params = params.copy()
        shifted_tape = tape.copy(copy_operations=True)

        if multipliers is not None:
            m = multipliers[i]
            new_params[idx] = new_params[idx] * qml.math.convert_like(m, new_params[idx])

        new_params[idx] = new_params[idx] + qml.math.convert_like(s, new_params[idx])
        shifted_tape.set_parameters(new_params)
        tapes.append(shifted_tape)

    return tapes


def generate_multishifted_tapes(tape, indices, shifts, multipliers=None):
    r"""Generate a list of tapes where multiple marked trainable parameters
    have been shifted simultaneously by the values given.

    Args:
        tape (.QuantumTape): input quantum tape
        indices (int): indices of the trainable parameters to shift
        shifts (Sequence[Sequence[float or int]]): Nested sequence of shift values
            with the same length as ``indices``. The length of the inner sequence
            determines how many parameter-shifted tapes are created.
        multipliers (Sequence[Sequence[float or int]]): Sequence of multiplier values
            matching the shape of ``shifts``. Each multiplier scales the corresponding
            gate parameter before the shift is applied. If not provided, the parameters
            will not be scaled.

    Returns:
        list[QuantumTape]: List of quantum tapes. Each tape has multiple parameters
            (indicated by ``indices``) shifted by the values of ``shifts``. The length
            of the returned list of tapes will match the second dimension of ``shifts``
            and ``multipliers``.
    """
    params = list(tape.get_parameters())
    multipliers = np.ones_like(shifts) if multipliers is None else multipliers
    tapes = []

    for shift, multiplier in zip(shifts, multipliers):
        new_params = params.copy()
        shifted_tape = tape.copy(copy_operations=True)
        for idx, s, m in zip(indices, shift, multiplier):
            dtype = new_params[idx].dtype
            new_params[idx] = new_params[idx] * qml.math.convert_like(m, new_params[idx])
            new_params[idx] = new_params[idx] + qml.math.convert_like(s, new_params[idx])
            new_params[idx] = qml.math.cast(new_params[idx], dtype)

        shifted_tape.set_parameters(new_params)
        tapes.append(shifted_tape)

    return tapes
