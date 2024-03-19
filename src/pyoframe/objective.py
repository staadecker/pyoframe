from typing import Literal
from pyoframe.constraints import Expressionable, Expression


class Objective(Expression):
    def __init__(
        self, expr: Expressionable, sense: Literal["minimize", "maximize"]
    ) -> None:
        """
        Examples
        --------
        >>> from pyoframe import Objective, Variable, Model, sum
        >>> m = Model()
        >>> m.a = Variable()
        >>> m.b = Variable({"dim1": [1, 2, 3]})
        >>> m.maximize = m.a + sum("dim1", m.b)
        >>> m.maximize
        <Expression size=1 dimensions={} terms=4>
        maximize: a + b[1] + b[2] + b[3]
        """
        expr = expr.to_expr()
        super().__init__(expr.to_expr().data, model=expr._model)
        assert (
            not self.dimensions
        ), "Objective cannot have any dimensions as it must be a single expression"
        assert sense in ("minimize", "maximize")
        self.sense = sense
