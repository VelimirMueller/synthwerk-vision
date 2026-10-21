from pydantic import BaseModel, Field


class ImageOut(BaseModel):
    id: str = Field(description="Server-generated image id")
    content_type: str
    size_bytes: int
    width: int
    height: int


class ImageInfo(BaseModel):
    sha256: str
    content_type: str
    size_bytes: int
    width: int
    height: int


class ModelInfo(BaseModel):
    name: str
    taxonomy_version: int


class LabelOut(BaseModel):
    id: str = Field(description="Stable label id (WordNet synset id for ImageNet labels)")
    label: str
    score: float = Field(ge=0, le=1, description="Probability of this exact label")
    topic: str = Field(description="Most specific topic the label belongs to")
    path: list[str] = Field(description="Root topic → … → topic → label")


class TopicOut(BaseModel):
    id: str
    parent: str | None
    depth: int = Field(description="0 for a root topic such as 'animal' or 'vehicle'")
    score: float = Field(ge=0, le=1, description="Sum of the probabilities of all labels below")


class CorrectionOut(BaseModel):
    feedback_id: int
    label: LabelOut = Field(description="The label a user gave this picture; score = model's")
    distance: int = Field(description="dHash bits between this picture and the corrected one")


class ClassificationOut(BaseModel):
    request_id: str
    image: ImageInfo
    model: ModelInfo
    primary: LabelOut = Field(description="Most likely label")
    topic: TopicOut = Field(description="Most likely root topic (the meta topic)")
    labels: list[LabelOut] = Field(description="Top-k labels, best first")
    topics: list[TopicOut] = Field(description="Topics above the minimum score, best first")
    uncertain: bool = Field(description="True when the root topic score is below the threshold")
    correction: CorrectionOut | None = Field(
        default=None,
        description="A user corrected this exact picture before (resized/re-encoded copies "
        "match). Prefer it over `primary`; the model's own scores are left untouched.",
    )
    timings_ms: dict[str, float]


class ErrorOut(BaseModel):
    detail: str


class LabelIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="Label shown in responses")
    topic: str = Field(min_length=1, max_length=80)
    prompt: str | None = Field(
        default=None, max_length=200, description="Text to embed instead of the name"
    )
    new_topic: bool = Field(default=False, description="Create `topic` under `topic_parent`")
    topic_parent: str | None = Field(
        default=None, description="Parent of the new topic; null = root"
    )


class RuntimeLabelOut(BaseModel):
    id: str
    name: str
    topic: str
    prompt: str | None = None
    path: list[str]


class RuntimeLabelsOut(BaseModel):
    labels: list[RuntimeLabelOut]
    total_labels: int = Field(description="All labels the model scores, packaged + runtime")


class FeedbackOut(BaseModel):
    id: int
    label_id: str
    dhash: str
