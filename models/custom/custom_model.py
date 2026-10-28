"""
The smallest possible model: no weights, just Python. Copy it to start a new
one; see models/README.md.

It also defines its own task, "custom": the models a task's requests are
checked against, and what every model for the task must return.
"""

from pydantic import Field

from models import BaseModel, register
from models.tasks import TaskInput, TaskOutput, define_task


class EchoInput(TaskInput):
    text: str = Field(..., min_length=1)


class EchoOutput(TaskOutput):
    text: str


define_task("custom", EchoInput, EchoOutput, "Echo the input, prefixed with the model ID")


@register(task="custom", match="custom:*")
class CustomModel(BaseModel):
    """Echoes its input. Serves any "custom:<name>" ID for the "custom" task."""

    def load(self):
        self.prefix = f"Processed by {self.model_id}"

    def predict(self, request: EchoInput) -> EchoOutput:
        return EchoOutput(text=f"{self.prefix}: {request.text}")
