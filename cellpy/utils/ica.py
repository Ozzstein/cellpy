"""ica contains routines for creating and working with incremental capacity analysis data"""

import os
import logging
import warnings

import numpy as np
from scipy import stats
from scipy.interpolate import interp1d
from scipy.signal import savgol_filter
from scipy.integrate import simps
from scipy.ndimage.filters import gaussian_filter1d
import pandas as pd

from cellpy.exceptions import NullData
from cellpy.readers.cellreader import _collect_capacity_curves


METHODS = ['linear', 'nearest', 'zero', 'slinear', 'quadratic', 'cubic']


# TODO: @jepe - documentation and tests
# TODO: @jepe - fitting of o-c curves and differentiation
# TODO: @jepe - modeling and fitting
# TODO: @jepe - full-cell
# TODO: @jepe - binning method (assigned to Asbjoern)


class Converter(object):
    """Class for dq-dv handling.

    Typical usage is to  (1) set the data,  (2) inspect the data, (3) pre-process the data,
    (4) perform the dq-dv transform, and finally (5) post-process the data.
    """

    def __init__(self):
        self.capacity = None
        self.voltage = None

        self.capacity_preprocessed = None
        self.voltage_preprocessed = None
        self.capacity_inverted = None
        self.voltage_inverted = None

        self.incremental_capacity = None
        self._incremental_capacity = None  # before smoothing
        self.voltage_processed = None

        self.voltage_inverted_step = None

        self.points_pr_split = 10
        self.minimum_splits = 3
        self.interpolation_method = 'linear'
        self.increment_method = 'diff'
        self.pre_smoothing = True
        self.smoothing = True
        self.post_smoothing = True
        self.savgol_filter_window_divisor_default = 50
        self.savgol_filter_window_order = 3
        self.voltage_fwhm = 0.01  # res voltage (peak-width)
        self.gaussian_order = 0
        self.gaussian_mode = "reflect"
        self.gaussian_cval = 0.0
        self.gaussian_truncate = 4.0
        self.normalise = True

        self.normalising_factor = None
        self.d_capacity_mean = None
        self.d_voltage_mean = None
        self.len_capacity = None
        self.len_voltage = None
        self.min_capacity = None
        self.max_capacity = None
        self.start_capacity = None
        self.end_capacity = None
        self.number_of_points = None
        self.std_err_median = None
        self.std_err_mean = None

        self.fixed_voltage_range = False

        self.errors = []

    def set_data(self, capacity, voltage=None,
                 capacity_label="q", voltage_label="v"
                 ):
        """Set the data"""

        if isinstance(capacity, pd.DataFrame):
            self.capacity = capacity[capacity_label]
            self.voltage = capacity[voltage_label]
        else:
            assert len(capacity) == len(voltage)
            self.capacity = capacity
            self.voltage = voltage

    def inspect_data(self, capacity=None, voltage=None):
        """check and inspect the data"""

        if capacity is None:
            capacity = self.capacity
        if voltage is None:
            voltage = self.voltage

        if capacity is None or voltage is None:
            raise NullData

        self.len_capacity = len(capacity)
        self.len_voltage = len(voltage)

        if self.len_capacity <= 1:
            raise NullData
        if self.len_voltage <= 1:
            raise NullData

        d_capacity = np.diff(capacity)
        d_voltage = np.diff(voltage)
        self.d_capacity_mean = np.mean(d_capacity)
        self.d_voltage_mean = np.mean(d_voltage)

        self.min_capacity, self.max_capacity = value_bounds(capacity)
        self.start_capacity, self.end_capacity = index_bounds(capacity)

        self.number_of_points = len(capacity)

        splits = int(self.number_of_points / self.points_pr_split)
        rest = self.number_of_points % self.points_pr_split

        if splits < self.minimum_splits:
            txt = "no point in splitting, too little data"
            logging.info(txt)
            self.errors.append("splitting: to few points")
        else:
            if rest > 0:
                _cap = capacity[:-rest]
                _vol = voltage[:-rest]
            else:
                _cap = capacity
                _vol = voltage

            c_pieces = np.split(_cap, splits)
            v_pieces = np.split(_vol, splits)
            # c_middle = int(np.amax(c_pieces) / 2)

            std_err = []
            c_pieces_avg = []
            for c, v in zip(c_pieces, v_pieces):
                _slope, _intercept, _r_value, _p_value, _std_err = stats.linregress(c, v)
                std_err.append(_std_err)
                c_pieces_avg.append(np.mean(c))

            self.std_err_median = np.median(std_err)
            self.std_err_mean = np.mean(std_err)

        if not self.start_capacity == self.min_capacity:
            self.errors.append("capacity: start<>min")
        if not self.end_capacity == self.max_capacity:
            self.errors.append("capacity: end<>max")
        self.normalising_factor = self.end_capacity

    def pre_process_data(self):
        """perform some pre-processing of the data (i.e. interpolation)"""
        capacity = self.capacity
        voltage = self.voltage
        len_capacity = self.len_capacity
        # len_voltage = self.len_voltage

        f = interp1d(capacity, voltage, kind=self.interpolation_method)
        c1, c2 = index_bounds(capacity)
        self.capacity_preprocessed = np.linspace(c1, c2, len_capacity)
        # capacity_step = (c2-c1)/(len_capacity-1)
        self.voltage_preprocessed = f(self.capacity_preprocessed)

        if self.pre_smoothing:
            savgol_filter_window_divisor = np.amin((self.savgol_filter_window_divisor_default, len_capacity / 5))
            savgol_filter_window_length = int(len_capacity / savgol_filter_window_divisor)

            if savgol_filter_window_length % 2 == 0:
                savgol_filter_window_length -= 1
            savgol_filter_window_length = np.amax([3, savgol_filter_window_length])

            self.voltage_preprocessed = savgol_filter(
                self.voltage_preprocessed,
                savgol_filter_window_length,
                self.savgol_filter_window_order
            )

    def increment_data(self):
        """perform the dq-dv transform"""

        # NOTE TO ASBJOERN: Probably insert method for "binning" instead of differentiating here
        # (use self.increment_method as the variable for selecting method for)

        # ---- shifting to y-x ----------------------------------------
        len_voltage = len(self.voltage_preprocessed)
        v1, v2 = value_bounds(self.voltage_preprocessed)

        # ---- interpolating ------------------------------------------
        f = interp1d(self.voltage_preprocessed, self.capacity_preprocessed, kind=self.interpolation_method)

        self.voltage_inverted = np.linspace(v1, v2, len_voltage)
        self.voltage_inverted_step = (v2 - v1) / (len(self.voltage_inverted, ) - 1)
        self.capacity_inverted = f(self.voltage_inverted)

        if self.smoothing:
            savgol_filter_window_divisor = np.amin((self.savgol_filter_window_divisor_default, len_voltage / 5))
            savgol_filter_window_length = int(len(self.voltage_inverted) / savgol_filter_window_divisor)
            if savgol_filter_window_length % 2 == 0:
                savgol_filter_window_length -= 1

            self.capacity_inverted = savgol_filter(self.capacity_inverted,
                                                   np.amax([3, savgol_filter_window_length]),
                                                   self.savgol_filter_window_order)

        # ---  diff --------------------
        if self.increment_method == "diff":
            self.incremental_capacity = np.ediff1d(self.capacity_inverted) / self.voltage_inverted_step
            self._incremental_capacity = self.incremental_capacity
            # --- need to adjust voltage ---
            self.voltage_processed = self.voltage_inverted[1:] + 0.5 * self.voltage_inverted_step  # centering

    def post_process_data(self, voltage=None, incremental_capacity=None,
                          voltage_step=None):
        """perform post-processing (smoothing, normalisation, interpolation) of
        the data"""

        if voltage is None:
            voltage = self.voltage_processed
            incremental_capacity = self.incremental_capacity
            voltage_step = self.voltage_inverted_step

        if self.post_smoothing:
            points_fwhm = int(self.voltage_fwhm / voltage_step)
            sigma = np.amax([2, points_fwhm / 2])
            self.incremental_capacity = gaussian_filter1d(
                incremental_capacity, sigma=sigma, order=self.gaussian_order,
                mode=self.gaussian_mode,
                cval=self.gaussian_cval, truncate=self.gaussian_truncate
            )

        if self.normalise:
            area = simps(incremental_capacity, voltage)
            self.incremental_capacity = incremental_capacity * self.normalising_factor / abs(area)

        fixed_range = False
        if isinstance(self.fixed_voltage_range, np.ndarray):
            fixed_range = True
        else:
            if self.fixed_voltage_range:
                fixed_range = True
        if fixed_range:
            v1, v2, number_of_points = self.fixed_voltage_range
            v = np.linspace(v1, v2, number_of_points)
            f = interp1d(x=self.voltage_processed, y=self.incremental_capacity,
                         kind=self.interpolation_method, bounds_error=False,
                         fill_value=np.NaN)

            self.incremental_capacity = f(v)
            self.voltage_processed = v


def value_bounds(x):
    """returns tuple with min and max in x"""
    return np.amin(x), np.amax(x)


def index_bounds(x):
    """returns tuple with first and last item in pandas Series x"""
    return x.iloc[0], x.iloc[-1]


def dqdv_cycle(cycle, splitter=True):
    """Convenience functions for creating dq-dv data from given capacity and
    voltage cycle.

    Returns the a DataFrame with a 'voltage' and a 'incremental_capacity'
    column.

        Args:
            cycle (pandas.DataFrame): the cycle data ('voltage', 'capacity',
                 'direction' (1 or -1)).
            splitter (bool): insert a np.NaN row between charge and discharge.

        Returns:
            List of step numbers corresponding to the selected steptype.
                Returns a pandas.DataFrame
            instead of a list if pdtype is set to True.

        Example:
            >>> cycle_df = my_data.get_cap(
            >>> ...   1,
            >>> ...   categorical_column=True,
            >>> ...   method = "forth-and-forth"
            >>> ... )
            >>> voltage, incremental = ica.dqdv_cycle(cycle_df)

    """

    c_first = cycle.loc[cycle["direction"] == -1]
    c_last = cycle.loc[cycle["direction"] == 1]

    converter = Converter()
    converter.set_data(c_first["capacity"], c_first["voltage"])
    converter.inspect_data()
    converter.pre_process_data()
    converter.increment_data()
    converter.post_process_data()
    voltage_first = converter.voltage_processed
    incremental_capacity_first = converter.incremental_capacity

    if splitter:
        voltage_first = np.append(voltage_first, np.NaN)
        incremental_capacity_first = np.append(incremental_capacity_first,
                                               np.NaN)

    converter = Converter()
    converter.set_data(c_last["capacity"], c_last["voltage"])
    converter.inspect_data()
    converter.pre_process_data()
    converter.increment_data()
    converter.post_process_data()
    voltage_last = converter.voltage_processed[::-1]
    incremental_capacity_last = converter.incremental_capacity[::-1]
    voltage = np.concatenate((voltage_first,
                              voltage_last))
    incremental_capacity = np.concatenate((incremental_capacity_first,
                                           incremental_capacity_last))

    return voltage, incremental_capacity


def dqdv_cycles(cycles):
    """Convenience functions for creating dq-dv data from given capacity and
    voltage cycles.

    Returns a DataFrame with a 'voltage' and a 'incremental_capacity'
    column.

        Args:
            cycles (pandas.DataFrame): the cycle data ('cycle', 'voltage',
                 'capacity', 'direction' (1 or -1)).

        Returns:
            pandas.DataFrame with columns 'cycle', 'voltage', 'dq'.

        Example:
            >>> cycles_df = my_data.get_cap(
            >>> ...   categorical_column=True,
            >>> ...   method = "forth-and-forth",
            >>> ...   label_cycle_number=True,
            >>> ... )
            >>> ica_df = ica.dqdv_cycles(cycles_df)

    """

    ica_dfs = list()
    cycle_group = cycles.groupby("cycle")
    for cycle_number, cycle in cycle_group:

        v, dq = dqdv_cycle(cycle, splitter=True)
        _ica_df = pd.DataFrame(
            {
                "voltage": v,
                "dq": dq,
            }
        )
        _ica_df["cycle"] = cycle_number
        _ica_df = _ica_df[['cycle', 'voltage', 'dq']]
        ica_dfs.append(_ica_df)

    ica_df = pd.concat(ica_dfs)
    return ica_df


def dqdv(voltage, capacity):
    """Convenience functions for creating dq-dv data from given capacity and
    voltage data"""

    converter = Converter()
    converter.set_data(capacity, voltage)
    converter.inspect_data()
    converter.pre_process_data()
    converter.increment_data()
    converter.post_process_data()
    return converter.voltage_processed, converter.incremental_capacity


def _constrained_dq_dv_using_dataframes(capacity, minimum_v, maximum_v):
    converter = Converter()
    converter.set_data(capacity)
    converter.inspect_data()
    converter.pre_process_data()
    converter.increment_data()
    converter.fixed_voltage_range = [minimum_v, maximum_v, 100]
    converter.post_process_data()
    return converter.voltage_processed, converter.incremental_capacity


def _make_ica_charge_curves(cycles_dfs, cycle_numbers, minimum_v, maximum_v):
    incremental_charge_list = []
    for c, n in zip(cycles_dfs, cycle_numbers):
        if not c.empty:
            v, dq = _constrained_dq_dv_using_dataframes(
                c, minimum_v, maximum_v
            )
            if not incremental_charge_list:
                d = pd.DataFrame({"v": v})
                d.name = "voltage"
                incremental_charge_list.append(d)

                d = pd.DataFrame({f"dq": dq})
                d.name = n
                incremental_charge_list.append(d)

            else:
                d = pd.DataFrame({f"dq": dq})
                # d.name = f"{cycle}"
                d.name = n
                incremental_charge_list.append(d)
        else:
            print(f"{n} is empty")
    return incremental_charge_list


def _dqdv_combinded_frame(cell):
    """Returns full cycle dqdv data for all cycles as one pd.DataFrame.

        Args:
            cell: CellpyData-object

        Returns:
            pandas.DataFrame with the following columns:
                cycle: cycle number
                voltage: voltage
                dq: the incremental capacity
    """

    cycles = cell.get_cap(
        method="forth-and-forth",
        categorical_column=True,
        label_cycle_number=True,
    )
    ica_df = dqdv_cycles(cycles)
    assert isinstance(ica_df, pd.DataFrame)
    return ica_df


def dqdv_frames(cell, split=False):
    """Returns dqdv data as pandas.DataFrame(s) for all cycles.

            Args:
                cell (CellpyData-object).
                split (bool): return one frame for charge and one for
                    discharge if True (defaults to False).

            Returns:
                pandas.DataFrame(s) with the following columns:
                    cycle: cycle number (if split is set to True).
                    voltage: voltage
                    dq: the incremental capacity

            Example:
                >>> from cellpy.utils import ica
                >>> charge_df, dcharge_df = ica.ica_frames(my_cell, split=True)
                >>> charge_df.plot(x=("voltage", "v"))
    """

    if split:
        return _dqdv_split_frames(cell, tidy=True)
    else:
        return _dqdv_combinded_frame(cell)


def _dqdv_split_frames(cell, tidy=False):
    """Returns dqdv data as pandas.DataFrames for all cycles.

        Args:
            cell (CellpyData-object).
            tidy (bool): return in wide format if False (default),
                long (tidy) format if True.

        Returns:
            (charge_ica_frame, discharge_ica_frame) where the frames are
            pandas.DataFrames where the first column is voltage ('v') and
            the following columns are the incremental capcaity for each
            cycle (multi-indexed, where cycle number is on the top level).

        Example:
            >>> from cellpy.utils import ica
            >>> charge_ica_df, dcharge_ica_df = ica.ica_frames(my_cell)
            >>> charge_ica_df.plot(x=("voltage", "v"))

    """
    charge_dfs, cycles, minimum_v, maximum_v = _collect_capacity_curves(
        cell,
        direction="charge"
    )
    # charge_df = pd.concat(
    # charge_dfs, axis=1, keys=[k.name for k in charge_dfs])

    ica_charge_dfs = _make_ica_charge_curves(
        charge_dfs, cycles, minimum_v, maximum_v
    )
    ica_charge_df = pd.concat(
        ica_charge_dfs,
        axis=1,
        keys=[k.name for k in ica_charge_dfs]
    )

    dcharge_dfs, cycles, minimum_v, maximum_v = _collect_capacity_curves(
        cell,
        direction="discharge"
    )
    ica_dcharge_dfs = _make_ica_charge_curves(
        dcharge_dfs, cycles, minimum_v, maximum_v
    )
    ica_discharge_df = pd.concat(
        ica_dcharge_dfs,
        axis=1,
        keys=[k.name for k in ica_dcharge_dfs]
    )
    ica_charge_df.columns.names = ["cycle", "value"]
    ica_discharge_df.columns.names = ["cycle", "value"]

    if tidy:
        ica_charge_df = ica_charge_df.melt(
            "voltage",
            var_name="cycle",
            value_name="dq",
            col_level=0
        )
        ica_discharge_df = ica_discharge_df.melt(
            "voltage",
            var_name="cycle",
            value_name="dq",
            col_level=0
        )

    return ica_charge_df, ica_discharge_df


def check_class_ica():
    print(40 * "=")
    print("running check_class_ica")
    print(40 * "-")

    import matplotlib.pyplot as plt

    cell = get_a_cell_to_play_with()
    cycle = 5
    print("looking at cycle %i" % cycle)

    # ---------- processing and plotting ----------------
    fig, (ax1, ax2) = plt.subplots(2, 1)
    capacity, voltage = cell.get_ccap(cycle)
    ax1.plot(capacity, voltage, "b.-", label="raw")
    converter = Converter()
    converter.set_data(capacity, voltage)
    converter.inspect_data()
    converter.pre_process_data()
    ax1.plot(converter.capacity_preprocessed, converter.voltage_preprocessed,
             "r.-", alpha=0.3, label="pre-processed")

    converter.increment_data()
    ax2.plot(converter.voltage_processed, converter.incremental_capacity,
             "b.-", label="incremented")

    converter.fixed_voltage_range = False
    converter.post_smoothing = True
    converter.normalise = False
    converter.post_process_data()
    ax2.plot(converter.voltage_processed, converter.incremental_capacity,
             "y-", alpha=0.3, lw=4.0, label="smoothed")

    converter.fixed_voltage_range = np.array((0.1, 1.2, 100))
    converter.post_smoothing = False
    converter.normalise = False
    converter.post_process_data()
    ax2.plot(converter.voltage_processed, converter.incremental_capacity,
             "go", alpha=0.7,
             label="fixed voltage range")
    ax1.legend(numpoints=1)
    ax2.legend(numpoints=1)
    ax1.set_ylabel("Voltage (V)")
    ax1.set_xlabel("Capacity (mAh/g)")
    ax2.set_xlabel("Voltage (V)")
    ax2.set_ylabel("dQ/dV (mAh/g/V)")
    plt.show()


def get_a_cell_to_play_with():
    # -------- defining overall path-names etc ----------
    current_file_path = os.path.dirname(os.path.realpath(__file__))
    print(current_file_path)
    relative_test_data_dir = "../../testdata/hdf5"
    test_data_dir = os.path.abspath(
        os.path.join(current_file_path, relative_test_data_dir))
    # test_data_dir_out = os.path.join(test_data_dir, "out")
    test_cellpy_file = "20160805_test001_45_cc.h5"
    test_cellpy_file_full = os.path.join(test_data_dir, test_cellpy_file)
    # mass = 0.078609164

    # ---------- loading test-data ----------------------
    cell = cellreader.CellpyData()
    cell.load(test_cellpy_file_full)
    list_of_cycles = cell.get_cycle_numbers()
    number_of_cycles = len(list_of_cycles)
    print("you have %i cycles" % number_of_cycles)
    #cell.save(test_cellpy_file_full)
    return cell


if __name__ == '__main__':
    # check_class_ica()¨
    import pandas as pd
    from cellpy import cellreader
    cell = get_a_cell_to_play_with()

    a = dqdv_frames(cell)
    # charge_df, discharge_df = ica_frames(cell)
