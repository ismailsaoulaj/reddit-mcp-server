from pydantic import BaseModel, Field


class RedditPost(BaseModel):
    """Represents a single Reddit post with time-awareness."""

    id: str = Field(..., description="The unique identifier of the post.")
    title: str = Field(..., description="The title of the post.")
    subreddit: str = Field(
        ..., description="The name of the subreddit where this was posted."
    )
    score: int = Field(..., description="The net upvotes.")
    upvote_ratio: float = Field(
        0.0, description="Consensus metric: 1.0 = Loved, 0.5 = Controversial."
    )
    num_comments: int = Field(..., description="Total comments.")
    url: str = Field(..., description="The direct URL to the post.")
    age_in_days: int | None = Field(
        ...,
        description=(
            "Days since post was created. None means the timestamp is unknown."
        ),
    )
    created_at_human: str = Field(
        ..., description="Human-readable date (e.g., 'October 15, 2023')."
    )
    text_preview: str = Field(..., description="A short preview of the post body.")


class RedditComment(BaseModel):
    """Represents a high-quality filtered comment."""

    id: str = Field(..., description="The comment identifier.")
    author: str = Field(..., description="The author's username.")
    score: int = Field(..., description="Net upvotes.")
    body: str = Field(..., description="Markdown content.")
    comment_url: str = Field(
        ..., description="Direct citation link. Use this for references."
    )
    created_at_human: str = Field(..., description="Human-readable date.")


class RedditThread(BaseModel):
    """Represents a full Reddit thread, including the main post and its top comments."""

    post: RedditPost = Field(..., description="The original Reddit post.")
    comments: list[RedditComment] = Field(
        ..., description="A list of comments associated with the post."
    )


class MetaContext(BaseModel):
    """Temporal and instructional context for the AI."""

    current_server_date: str = Field(..., description="The current server date.")
    instruction_note: str = Field(..., description="Guiding instruction for the LLM.")


class PaginatedPostResponse(BaseModel):
    """A paginated list of Reddit posts wrapped with meta-context."""

    meta_context: MetaContext = Field(
        ..., description="Temporal and spatial context for the AI."
    )
    data: list[RedditPost] = Field(..., description="The extracted posts.")
    next_page_token: str | None = Field(
        None, description="Pass this token to the tool again to fetch the next page."
    )
    status: str = Field(
        "success", description="Status of the request (e.g., success, partial_timeout)."
    )
    message: str | None = Field(
        None,
        description="System message or warning (especially if partial_timeout occurred).",
    )
    data_source: str | None = Field(
        None,
        description=(
            "Provenance of the data: None = official Reddit API, "
            "'arctic_shift' = community archive (metrics may lag live Reddit), "
            "'saved_rss' = the user's private saved-items feed "
            "(scores/comment counts unavailable)."
        ),
    )


class PaginatedCommentResponse(BaseModel):
    """A list of comments wrapped with meta-context."""

    meta_context: MetaContext = Field(
        ..., description="Temporal and spatial context for the AI."
    )
    data: list[RedditComment] = Field(..., description="The extracted comments.")
    next_page_token: str | None = Field(
        None,
        description=(
            "Pass this token to the tool again to fetch the next page of comments."
        ),
    )
    status: str = Field("success", description="Status of the request.")
    message: str | None = Field(None, description="System message or warning.")
    data_source: str | None = Field(
        None,
        description=(
            "Provenance of the data: None = official Reddit API, "
            "'arctic_shift' = community archive (metrics may lag live Reddit), "
            "'saved_rss' = the user's private saved-items feed "
            "(scores/comment counts unavailable)."
        ),
    )
