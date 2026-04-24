class Client:
    """Hail API client (alpha placeholder — full client lands in v1)."""

    def __init__(self, api_key: str, base_url: str = "https://api.hail.so") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"<hail.Client base_url={self.base_url!r}>"
