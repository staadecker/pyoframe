from __future__ import annotations
from typing import (
    TYPE_CHECKING,
    Iterable,
    List,
    Mapping,
    Protocol,
    Sequence,
    overload,
    Union,
)
from abc import ABC, abstractmethod

import pandas as pd
import polars as pl

from pyoframe.arithmetic import add_expressions, get_dimensions
from pyoframe.constants import Config
from pyoframe.constants import (
    COEF_KEY,
    CONST_TERM,
    RESERVED_COL_KEYS,
    VAR_KEY,
    ConstraintSense,
    UnmatchedStrategy,
)
from pyoframe.util import (
    cast_coef_to_string,
    concat_dimensions,
    get_obj_repr,
    parse_inputs_as_iterable,
)
from pyoframe.var_mapping import DEFAULT_MAP
from pyoframe.model_element import ModelElement

if TYPE_CHECKING:
    from pyoframe.model import Model

VAR_TYPE = pl.UInt32


def _forward_to_expression(func_name: str):
    def wrapper(self: "SupportsMath", *args, **kwargs) -> "Expression":
        expr = self.to_expr()
        return getattr(expr, func_name)(*args, **kwargs)

    return wrapper


class SupportsToExpr(Protocol):
    def to_expr(self) -> "Expression": ...


class SupportsMath(ABC, SupportsToExpr):
    """Any object that can be converted into an expression."""

    def __init__(self):
        self.unmatched_strategy = UnmatchedStrategy.ERROR if Config.default_to_error_on_missing else UnmatchedStrategy.KEEP
        self.allowed_new_dims: List[str] = []

    def keep_unmatched(self):
        self.unmatched_strategy = UnmatchedStrategy.KEEP
        return self

    def drop_unmatched(self):
        self.unmatched_strategy = UnmatchedStrategy.DROP
        return self

    def add_dim(self, *dims: str):
        self.allowed_new_dims.extend(dims)
        return self

    @abstractmethod
    def to_expr(self) -> "Expression":
        raise NotImplementedError

    __add__ = _forward_to_expression("__add__")
    __mul__ = _forward_to_expression("__mul__")
    sum = _forward_to_expression("sum")

    def __neg__(self):
        return self.to_expr() * -1

    def __sub__(self, other):
        """
        >>> import polars as pl
        >>> from pyoframe import Variable
        >>> df = pl.DataFrame({"dim1": [1,2,3], "value": [1,2,3]})
        >>> var = Variable(df["dim1"])
        >>> var - df
        <Expression size=3 dimensions={'dim1': 3} terms=6>
        [1]: x1 -1
        [2]: x2 -2
        [3]: x3 -3
        """
        if not isinstance(other, (int, float)):
            other = other.to_expr()
        return self.to_expr() + (other * -1)

    def __rmul__(self, other):
        return self.to_expr() * other

    def __radd__(self, other):
        return self.to_expr() + other

    def __le__(self, other):
        """Equality constraint.
        Examples
        >>> from pyoframe import Variable
        >>> Variable() <= 1
        <Constraint sense='<=' size=1 dimensions={} terms=2>
        x1 <= 1
        """
        return Constraint(self - other, ConstraintSense.LE)

    def __ge__(self, other):
        """Equality constraint.
        Examples
        >>> from pyoframe import Variable
        >>> Variable() >= 1
        <Constraint sense='>=' size=1 dimensions={} terms=2>
        x1 >= 1
        """
        return Constraint(self - other, ConstraintSense.GE)

    def __eq__(self, __value: object):
        """Equality constraint.
        Examples
        >>> from pyoframe import Variable
        >>> Variable() == 1
        <Constraint sense='=' size=1 dimensions={} terms=2>
        x1 = 1
        """
        return Constraint(self - __value, ConstraintSense.EQ)


SetTypes = Union[
    pl.DataFrame,
    pd.Index,
    pd.DataFrame,
    SupportsMath,
    Mapping[str, Sequence[object]],
    "Set",
]


class Set(ModelElement, SupportsMath):
    def __init__(self, *data: SetTypes | Iterable[SetTypes], **named_data):
        data_list = list(data)
        for name, set in named_data.items():
            data_list.append({name: set})
        df = self._parse_acceptable_sets(*data_list)
        if df.is_duplicated().any():
            raise ValueError("Duplicate rows found in input data.")
        super().__init__(df)

    def _new(self, data: pl.DataFrame):
        s = Set(data)
        s._model = self._model
        s.unmatched_strategy = self.unmatched_strategy
        return s

    @staticmethod
    def _parse_acceptable_sets(
        *over: SetTypes | Iterable[SetTypes],
    ) -> pl.DataFrame:
        """
        >>> import pandas as pd
        >>> dim1 = pd.Index([1, 2, 3], name="dim1")
        >>> dim2 = pd.Index(["a", "b"], name="dim1")
        >>> Set._parse_acceptable_sets([dim1, dim2])
        Traceback (most recent call last):
        ...
        AssertionError: All coordinates must have unique column names.
        >>> dim2.name = "dim2"
        >>> Set._parse_acceptable_sets([dim1, dim2])
        shape: (6, 2)
        ┌──────┬──────┐
        │ dim1 ┆ dim2 │
        │ ---  ┆ ---  │
        │ i64  ┆ str  │
        ╞══════╪══════╡
        │ 1    ┆ a    │
        │ 1    ┆ b    │
        │ 2    ┆ a    │
        │ 2    ┆ b    │
        │ 3    ┆ a    │
        │ 3    ┆ b    │
        └──────┴──────┘
        """
        assert len(over) > 0, "At least one set must be provided."
        over_iter: Iterable[SetTypes] = parse_inputs_as_iterable(*over)

        over_frames: List[pl.DataFrame] = [Set._set_to_polars(set) for set in over_iter]

        over_merged = over_frames[0]

        for df in over_frames[1:]:
            assert (
                set(over_merged.columns) & set(df.columns) == set()
            ), "All coordinates must have unique column names."
            over_merged = over_merged.join(df, how="cross")
        return over_merged

    def to_expr(self) -> Expression:
        return Expression(
            data=self.data.with_columns(
                pl.lit(1).alias(COEF_KEY), pl.lit(CONST_TERM).alias(VAR_KEY)
            )
        )

    def __mul__(self, other):
        if isinstance(other, Set):
            assert (
                set(self.data.columns) & set(other.data.columns) == set()
            ), "Cannot multiply two sets with columns in common."
            return Set(self.data, other.data)
        return super().__mul__(other)

    def __add__(self, other):
        if isinstance(other, Set):
            raise ValueError("Cannot add two sets.")
        return super().__add__(other)

    def __repr__(self):
        return (
            get_obj_repr(self, ("name",), size=self.data.height, dimensions=self.shape)
            + "\n"
            + self.to_expr().to_str(max_line_len=80, max_rows=10)
        )

    @staticmethod
    def _set_to_polars(set: "SetTypes") -> pl.DataFrame:
        if isinstance(set, dict):
            df = pl.DataFrame(set)
        elif isinstance(set, SupportsMath):
            df = set.to_expr().data.drop(RESERVED_COL_KEYS).unique(maintain_order=True)
        elif isinstance(set, pd.Index):
            df = pl.from_pandas(pd.DataFrame(index=set).reset_index())
        elif isinstance(set, pd.DataFrame):
            df = pl.from_pandas(set)
        elif isinstance(set, pl.DataFrame):
            df = set
        elif isinstance(set, pl.Series):
            df = set.to_frame()
        elif isinstance(set, Set):
            df = set.data
        else:
            raise ValueError(f"Cannot convert type {type(set)} to a polars DataFrame")

        if "index" in df.columns:
            raise ValueError(
                "Please specify a custom dimension name rather than using 'index' to avoid confusion."
            )

        for reserved_key in RESERVED_COL_KEYS:
            if reserved_key in df.columns:
                raise ValueError(
                    f"Cannot use reserved column names {reserved_key} as dimensions."
                )

        return df


class Expression(ModelElement, SupportsMath):
    """A linear expression."""

    def __init__(self, data: pl.DataFrame):
        """
        >>> import pandas as pd
        >>> from pyoframe import Variable, Model
        >>> df = pd.DataFrame({"item" : [1, 1, 1, 2, 2], "time": ["mon", "tue", "wed", "mon", "tue"], "cost": [1, 2, 3, 4, 5]}).set_index(["item", "time"])
        >>> m = Model()
        >>> m.Time = Variable(df.index)
        >>> m.Size = Variable(df.index)
        >>> expr = df["cost"] * m.Time + df["cost"] * m.Size
        >>> expr
        <Expression size=5 dimensions={'item': 2, 'time': 3} terms=10>
        [1,mon]: Time[1,mon] + Size[1,mon]
        [1,tue]: 2 Time[1,tue] +2 Size[1,tue]
        [1,wed]: 3 Time[1,wed] +3 Size[1,wed]
        [2,mon]: 4 Time[2,mon] +4 Size[2,mon]
        [2,tue]: 5 Time[2,tue] +5 Size[2,tue]
        """
        # Sanity checks, VAR_KEY and COEF_KEY must be present
        assert VAR_KEY in data.columns, "Missing variable column."
        assert COEF_KEY in data.columns, "Missing coefficient column."

        # Sanity check no duplicates indices
        if data.drop(COEF_KEY).is_duplicated().any():
            duplicated_data = data.filter(data.drop(COEF_KEY).is_duplicated())
            raise ValueError(f"Duplicate indices found:\n{duplicated_data}.")

        super().__init__(data)

    def sum(self, over: str | Iterable[str]):
        """
        Examples
        --------
        >>> import pandas as pd
        >>> from pyoframe import Variable
        >>> df = pd.DataFrame({"item" : [1, 1, 1, 2, 2], "time": ["mon", "tue", "wed", "mon", "tue"], "cost": [1, 2, 3, 4, 5]}).set_index(["item", "time"])
        >>> quantity = Variable(df.reset_index()[["item"]].drop_duplicates())
        >>> expr = (quantity * df["cost"]).sum("time")
        >>> expr.data
        shape: (2, 3)
        ┌──────┬─────────┬───────────────┐
        │ item ┆ __coeff ┆ __variable_id │
        │ ---  ┆ ---     ┆ ---           │
        │ i64  ┆ f64     ┆ u32           │
        ╞══════╪═════════╪═══════════════╡
        │ 1    ┆ 6.0     ┆ 1             │
        │ 2    ┆ 9.0     ┆ 2             │
        └──────┴─────────┴───────────────┘
        """
        if isinstance(over, str):
            over = [over]
        dims = self.dimensions
        if not dims:
            raise ValueError(
                f"Cannot sum over dimensions {over} since the current expression has no dimensions."
            )
        assert set(over) <= set(dims), f"Cannot sum over {over} as it is not in {dims}"
        remaining_dims = [dim for dim in dims if dim not in over]

        return self._new(
            self.data.drop(over)
            .group_by(remaining_dims + [VAR_KEY], maintain_order=True)
            .sum()
        )

    def rolling_sum(self, over: str, window_size: int):
        """
        Calculates the rolling sum of the Expression over a specified window size for a given dimension.

        This method applies a rolling sum operation over the dimension specified by `over`,
        using a window defined by `window_size`.


        Parameters
        ----------
        over : str
               The name of the dimension (column) over which the rolling sum is calculated.
               This dimension must exist within the Expression's dimensions.
        window_size : int
               The size of the moving window in terms of number of records.
               The rolling sum is calculated over this many consecutive elements.

        Returns
        -------
        Expression
               A new Expression instance containing the result of the rolling sum operation.
               This new Expression retains all dimensions (columns) of the original data,
               with the rolling sum applied over the specified dimension.

        Examples
        --------
        >>> import polars as pl
        >>> from pyoframe import Variable, Model
        >>> cost = pl.DataFrame({"item" : [1, 1, 1, 2, 2], "time": [1, 2, 3, 1, 2], "cost": [1, 2, 3, 4, 5]})
        >>> m = Model()
        >>> m.quantity = Variable(cost[["item", "time"]])
        >>> (m.quantity * cost).rolling_sum(over="time", window_size=2)
        <Expression size=5 dimensions={'item': 2, 'time': 3} terms=8>
        [1,1]: quantity[1,1]
        [1,2]: quantity[1,1] +2 quantity[1,2]
        [1,3]: 2 quantity[1,2] +3 quantity[1,3]
        [2,1]: 4 quantity[2,1]
        [2,2]: 4 quantity[2,1] +5 quantity[2,2]
        """
        dims = self.dimensions
        if dims is None:
            raise ValueError(
                "Cannot use rolling_sum() with an expression with no dimensions."
            )
        assert over in dims, f"Cannot sum over {over} as it is not in {dims}"
        remaining_dims = [dim for dim in dims if dim not in over]

        return self._new(
            pl.concat(
                [
                    df.with_columns(pl.col(over).max())
                    for _, df in self.data.rolling(
                        index_column=over, period=f"{window_size}i", by=remaining_dims
                    )
                ]
            )
        )

    def within(self, set: "SetTypes") -> Expression:
        """
        Examples
        >>> import pandas as pd
        >>> general_expr = pd.DataFrame({"dim1": [1, 2, 3], "value": [1, 2, 3]}).to_expr()
        >>> filter_expr = pd.DataFrame({"dim1": [1, 3], "value": [5, 6]}).to_expr()
        >>> general_expr.within(filter_expr).data
        shape: (2, 3)
        ┌──────┬─────────┬───────────────┐
        │ dim1 ┆ __coeff ┆ __variable_id │
        │ ---  ┆ ---     ┆ ---           │
        │ i64  ┆ f64     ┆ u32           │
        ╞══════╪═════════╪═══════════════╡
        │ 1    ┆ 1.0     ┆ 0             │
        │ 3    ┆ 3.0     ┆ 0             │
        └──────┴─────────┴───────────────┘
        """
        df: pl.DataFrame = Set(set).data
        set_dims = get_dimensions(df)
        assert (
            set_dims is not None
        ), "Cannot use .within() with a set with no dimensions."
        dims = self.dimensions
        assert (
            dims is not None
        ), "Cannot use .within() with an expression with no dimensions."
        dims_in_common = [dim for dim in dims if dim in set_dims]
        by_dims = df.select(dims_in_common).unique(maintain_order=True)
        return self._new(self.data.join(by_dims, on=dims_in_common))

    def __add__(self, other):
        """
        Examples
        --------
        >>> import pandas as pd
        >>> from pyoframe import Variable
        >>> add = pd.DataFrame({"dim1": [1,2,3], "add": [10, 20, 30]}).set_index("dim1")["add"]
        >>> var = Variable(add.index)
        >>> expr = var + add
        >>> expr.data
        shape: (6, 3)
        ┌──────┬─────────┬───────────────┐
        │ dim1 ┆ __coeff ┆ __variable_id │
        │ ---  ┆ ---     ┆ ---           │
        │ i64  ┆ f64     ┆ u32           │
        ╞══════╪═════════╪═══════════════╡
        │ 1    ┆ 1.0     ┆ 1             │
        │ 2    ┆ 1.0     ┆ 2             │
        │ 3    ┆ 1.0     ┆ 3             │
        │ 1    ┆ 10.0    ┆ 0             │
        │ 2    ┆ 20.0    ┆ 0             │
        │ 3    ┆ 30.0    ┆ 0             │
        └──────┴─────────┴───────────────┘
        >>> expr += 2
        >>> expr.data
        shape: (6, 3)
        ┌──────┬─────────┬───────────────┐
        │ dim1 ┆ __coeff ┆ __variable_id │
        │ ---  ┆ ---     ┆ ---           │
        │ i64  ┆ f64     ┆ u32           │
        ╞══════╪═════════╪═══════════════╡
        │ 1    ┆ 1.0     ┆ 1             │
        │ 2    ┆ 1.0     ┆ 2             │
        │ 3    ┆ 1.0     ┆ 3             │
        │ 1    ┆ 12.0    ┆ 0             │
        │ 2    ┆ 22.0    ┆ 0             │
        │ 3    ┆ 32.0    ┆ 0             │
        └──────┴─────────┴───────────────┘
        >>> expr += pd.DataFrame({"dim1": [1,2], "add": [10, 20]}).set_index("dim1")["add"]
        >>> expr.data
        shape: (6, 3)
        ┌──────┬─────────┬───────────────┐
        │ dim1 ┆ __coeff ┆ __variable_id │
        │ ---  ┆ ---     ┆ ---           │
        │ i64  ┆ f64     ┆ u32           │
        ╞══════╪═════════╪═══════════════╡
        │ 1    ┆ 1.0     ┆ 1             │
        │ 2    ┆ 1.0     ┆ 2             │
        │ 3    ┆ 1.0     ┆ 3             │
        │ 1    ┆ 22.0    ┆ 0             │
        │ 2    ┆ 42.0    ┆ 0             │
        │ 3    ┆ 32.0    ┆ 0             │
        └──────┴─────────┴───────────────┘
        >>> expr = 5 + 2 * Variable()
        >>> expr
        <Expression size=1 dimensions={} terms=2>
        2 x4 +5
        """
        if isinstance(other, (int, float)):
            return self._add_const(other)
        other = other.to_expr()
        self._learn_from_other(other)
        return add_expressions(self, other)

    def __mul__(
        self: "Expression", other: int | float | SupportsToExpr
    ) -> "Expression":
        if isinstance(other, (int, float)):
            return self.with_columns(pl.col(COEF_KEY) * other)

        other = other.to_expr()
        self._learn_from_other(other)

        if (other.data.get_column(VAR_KEY) != CONST_TERM).any():
            self, other = other, self

        if (other.data.get_column(VAR_KEY) != CONST_TERM).any():
            raise ValueError(
                "Multiplication of two expressions with variables is non-linear and not supported."
            )
        multiplier = other.data.drop(VAR_KEY)

        dims = self.dimensions_unsafe
        other_dims = other.dimensions_unsafe
        dims_in_common = [dim for dim in dims if dim in other_dims]

        data = (
            self.data.join(
                multiplier,
                on=dims_in_common,
                how="inner" if dims_in_common else "cross",
            )
            .with_columns(pl.col(COEF_KEY) * pl.col(COEF_KEY + "_right"))
            .drop(COEF_KEY + "_right")
        )

        return self._new(data)

    def to_expr(self) -> Expression:
        return self

    def _learn_from_other(self, other: Expression):
        if self._model is None and other._model is not None:
            self._model = other._model

    def _new(self, data: pl.DataFrame) -> Expression:
        e = Expression(data)
        e._model = self._model
        e.unmatched_strategy = self.unmatched_strategy
        e.allowed_new_dims = self.allowed_new_dims
        return e

    def _add_const(self, const: int | float) -> Expression:
        dim = self.dimensions
        data = self.data
        # Fill in missing constant terms
        if not dim:
            if CONST_TERM not in data[VAR_KEY]:
                data = pl.concat(
                    [
                        data,
                        pl.DataFrame(
                            {COEF_KEY: [0.0], VAR_KEY: [CONST_TERM]},
                            schema={COEF_KEY: pl.Float64, VAR_KEY: VAR_TYPE},
                        ),
                    ],
                    how="vertical_relaxed",
                )
        else:
            keys = (
                data.select(dim)
                .unique(maintain_order=True)
                .with_columns(pl.lit(CONST_TERM).alias(VAR_KEY).cast(VAR_TYPE))
            )
            data = data.join(keys, on=dim + [VAR_KEY], how="outer_coalesce")
            data = data.with_columns(pl.col(COEF_KEY).fill_null(0.0))

        data = data.with_columns(
            pl.when(pl.col(VAR_KEY) == CONST_TERM)
            .then(pl.col(COEF_KEY) + const)
            .otherwise(pl.col(COEF_KEY))
        )

        return self._new(data)

    @property
    def constant_terms(self):
        dims = self.dimensions
        constant_terms = self.data.filter(pl.col(VAR_KEY) == CONST_TERM).drop(VAR_KEY)
        if dims is not None:
            return constant_terms.join(
                self.data.select(dims).unique(maintain_order=True), on=dims, how="outer_coalesce"
            ).with_columns(pl.col(COEF_KEY).fill_null(0.0))
        else:
            if len(constant_terms) == 0:
                return pl.DataFrame(
                    {COEF_KEY: [0.0], VAR_KEY: [CONST_TERM]},
                    schema={COEF_KEY: pl.Float64, VAR_KEY: VAR_TYPE},
                )
            return constant_terms

    @property
    def variable_terms(self):
        return self.data.filter(pl.col(VAR_KEY) != CONST_TERM)

    def to_str_table(
        self,
        max_line_len=None,
        max_rows=None,
        include_const_term=True,
        var_map=None,
        include_name=True,
    ):
        data = self.data if include_const_term else self.variable_terms
        if var_map is None:
            var_map = self._model.var_map if self._model is not None else DEFAULT_MAP
        data = cast_coef_to_string(data)
        data = var_map.map_vars(data)
        dimensions = self.dimensions

        # Create a string for each term
        data = data.with_columns(
            expr=pl.concat_str(
                COEF_KEY,
                pl.lit(" "),
                VAR_KEY,
            )
        ).drop(COEF_KEY, VAR_KEY)

        # Combine terms into one string
        if dimensions is not None:
            data = data.group_by(dimensions, maintain_order=True).agg(
                pl.col("expr").str.concat(delimiter=" ")
            )
        else:
            data = data.select(pl.col("expr").str.concat(delimiter=" "))

        # Remove leading +
        data = data.with_columns(pl.col("expr").str.strip_chars(characters=" +"))

        # TODO add vertical ... if too many rows, in the middle of the table
        if max_rows:
            data = data.head(max_rows)

        if max_line_len:
            data = data.with_columns(
                pl.when(pl.col("expr").str.len_chars() > max_line_len)
                .then(
                    pl.concat_str(
                        pl.col("expr").str.slice(0, max_line_len),
                        pl.lit("..."),
                    )
                )
                .otherwise(pl.col("expr"))
            )

        # Prefix with the dimensions
        prefix = (
            getattr(self, "name") if hasattr(self, "name") and include_name else None
        )
        if prefix or dimensions:
            data = concat_dimensions(data, prefix=prefix, ignore_columns=["expr"])
            data = data.with_columns(
                pl.concat_str(
                    pl.col("concated_dim"), pl.lit(": "), pl.col("expr")
                ).alias("expr")
            ).drop("concated_dim")

        return data

    def to_str(
        self,
        max_line_len=None,
        max_rows=None,
        include_const_term=True,
        var_map=None,
        include_name=True,
    ):
        str_table = self.to_str_table(
            max_line_len=max_line_len,
            max_rows=max_rows,
            include_const_term=include_const_term,
            var_map=var_map,
            include_name=include_name,
        )
        result = str_table.select(pl.col("expr").str.concat(delimiter="\n")).item()

        return result

    def __repr__(self) -> str:
        return (
            get_obj_repr(
                self, size=len(self), dimensions=self.shape, terms=len(self.data)
            )
            + "\n"
            + self.to_str(max_line_len=80, max_rows=15)
        )


@overload
def sum(over: str | Sequence[str], expr: SupportsToExpr): ...


@overload
def sum(over: SupportsToExpr): ...


def sum(
    over: str | Sequence[str] | SupportsToExpr,
    expr: SupportsToExpr | None = None,
) -> "Expression":
    if expr is None:
        assert isinstance(over, SupportsMath)
        over = over.to_expr()
        all_dims = over.dimensions
        if all_dims is None:
            raise ValueError(
                "Cannot sum over dimensions with an expression with no dimensions."
            )
        return over.sum(all_dims)
    else:
        assert isinstance(over, (str, Sequence))
        return expr.to_expr().sum(over)


def sum_by(by: str | Sequence[str], expr: SupportsToExpr) -> "Expression":
    if isinstance(by, str):
        by = [by]
    expr = expr.to_expr()
    dimensions = expr.dimensions
    assert dimensions is not None, "Cannot sum by dimensions with an expression with no dimensions."
    remaining_dims = [dim for dim in dimensions if dim not in by]
    return sum(over=remaining_dims, expr=expr)

def sum_list(*elist: Iterable[SupportsToExpr] | SupportsToExpr) -> Expression:
    """
    Sums a list of expressionable elements. For large elements, this can be
    significantly faster than creating an explicit sequence of additions
    e1 + e2 + e3 ...

    Examples
    --------
    >>> import numpy as np
    >>> import polars as pl
    >>> from pyoframe import Model, Variable
    >>> from pyoframe.constraints import sum_list
    >>> m = Model()
    >>> p = pl.DataFrame({"p": np.arange(3000)})
    >>> t = pl.DataFrame({"t": np.arange(200)})
    >>> for v in "xyzw":
    ...    setattr(m, v, Variable(p, t))
    >>> sl = sum_list([m.x, m.y, m.z, m.w])
    >>> sa = m.x + m.y + m.z + m.w
    >>> assert sl.data.sort("p", "t").equals(sa.data.sort("p", "t"))
    >>> sl.filter(p=42, t=42)
    <Expression size=1 dimensions={'p': 1, 't': 1} terms=4>
    [42,42]: x[42,42] + y[42,42] + z[42,42] + w[42,42]
    """

    assert len(elist) > 0, "At least one element must be provided."
    elist: List[Expression] = [el.to_expr() for el in parse_inputs_as_iterable(*elist)]

    # Check that dimensions are consistent
    dims = elist[0].dimensions
    if dims is None:
        for el in elist[1:]:
            if el.dimensions is not None:
                raise ValueError("Cannot sum expressions since an element is being summed when .")

    for el in elist[1:]:
        dims_other = el.dimensions
        if ((dims is None) != (dims_other is None)) or dims != set(dims_other):
            raise ValueError(f"Incompatible dimensions: {dims}, {el.dimensions}")

    return elist[0]._new(
        pl.concat([
            el.data for el in elist
        ],
        how="vertical_relaxed")
    )

class Constraint(Expression):
    def __init__(self, lhs: Expression | pl.DataFrame, sense: ConstraintSense):
        """Adds a constraint to the model.

        Parameters
        ----------
        data: Expression
            The left hand side of the constraint.
        sense: Sense
            The sense of the constraint.
        rhs: Expression
            The right hand side of the constraint.
        """
        if isinstance(lhs, Expression):
            data = lhs.data
        else:
            data = lhs
        super().__init__(data)
        if isinstance(lhs, Expression):
            self._model = lhs._model
        self.sense = sense

    def to_str(self, max_line_len=None, max_rows=None, var_map=None):
        dims = self.dimensions
        str_table = self.to_str_table(
            max_line_len=max_line_len,
            max_rows=max_rows,
            include_const_term=False,
            var_map=var_map,
        )
        rhs = self.constant_terms.with_columns(pl.col(COEF_KEY) * -1)
        rhs = cast_coef_to_string(rhs, drop_ones=False)
        # Remove leading +
        rhs = rhs.with_columns(pl.col(COEF_KEY).str.strip_chars(characters=" +"))
        rhs = rhs.rename({COEF_KEY: "rhs"})
        constr_str = pl.concat(
            [str_table, rhs], how=("align" if dims else "horizontal")
        )
        constr_str = constr_str.select(
            pl.concat_str("expr", pl.lit(f" {self.sense.value} "), "rhs").str.concat(
                delimiter="\n"
            )
        ).item()
        return constr_str

    def __repr__(self) -> str:
        return (
            get_obj_repr(
                self,
                ("name",),
                sense=f"'{self.sense.value}'",
                size=len(self),
                dimensions=self.shape,
                terms=len(self.data),
            )
            + "\n"
            + self.to_str(max_line_len=80, max_rows=15)
        )

    def _new(self, data: pl.DataFrame):
        c = Constraint(data, self.sense)
        c._model = self._model
        return c
