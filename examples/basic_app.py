"""Run with: python examples/basic_app.py

Try:
  curl http://127.0.0.1:8080/health
  curl 'http://127.0.0.1:8080/api/items?limit=5'
  curl http://127.0.0.1:8080/docs
"""

from vedrakit import App, BaseModel, Query


class ItemCreate(BaseModel):
    name: str
    quantity: int


app = App()


@app.route("/api/items", ["GET"])
def list_items(limit: int = Query(default=10)):
    """List the first items."""
    return {
        "items": [{"id": 1, "name": "Example item"}],
        "limit": limit,
    }


@app.route("/api/items", ["POST"])
def create_item(item: ItemCreate):
    """Validate and echo a new item."""
    return {"created": item.dict()}


if __name__ == "__main__":
    app.run(port=8080, production=False)