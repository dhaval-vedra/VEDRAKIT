"""A complete, dependency-free CRUD API.

Run:
    python -m examples.complete_app

Try:
    curl http://127.0.0.1:8080/api/todos
    curl -X POST http://127.0.0.1:8080/api/todos \
      -H 'Content-Type: application/json' \
      -d '{"title":"Ship Vedrakit","done":false}'
    vedrakit openapi examples.complete_app:app --output openapi.json
    vedrakit client examples.complete_app:app --output examples/todos-client.ts
"""

from vedrakit import App, BaseModel, Query


class TodoCreate(BaseModel):
    title: str
    done: bool = False


class Todo(TodoCreate):
    id: int


class TodoList(BaseModel):
    items: list[Todo]
    limit: int


app = App(
    title="Todo API",
    version="1.0.0",
    description="A complete CRUD example using models, validation, query parameters, and OpenAPI.",
)

todos: list[Todo] = []


@app.get("/api/todos", response_model=TodoList, tags=["todos"], summary="List todos")
def list_todos(limit: int = Query(default=20, description="Maximum number of todos")):
    """Return the current todo collection."""
    return {"items": [todo.dict() for todo in todos[:limit]], "limit": limit}


@app.post("/api/todos", response_model=Todo, tags=["todos"], summary="Create a todo")
def create_todo(todo: TodoCreate):
    """Validate and add a todo."""
    created = Todo.parse_obj({"id": len(todos) + 1, "title": todo.title, "done": todo.done})
    todos.append(created)
    return 201, created.dict()


@app.get("/api/todos/{todo_id}", response_model=Todo, tags=["todos"], summary="Get a todo")
def get_todo(todo_id: int):
    """Return one todo or a clear not-found error."""
    for todo in todos:
        if todo.id == todo_id:
            return todo.dict()
    return 404, {"error": "Todo not found", "todo_id": todo_id}


@app.put("/api/todos/{todo_id}", response_model=Todo, tags=["todos"], summary="Replace a todo")
def replace_todo(todo_id: int, todo: TodoCreate):
    """Replace an existing todo."""
    for index, current in enumerate(todos):
        if current.id == todo_id:
            updated = Todo.parse_obj({"id": todo_id, "title": todo.title, "done": todo.done})
            todos[index] = updated
            return updated.dict()
    return 404, {"error": "Todo not found", "todo_id": todo_id}


@app.delete("/api/todos/{todo_id}", tags=["todos"], summary="Delete a todo")
def delete_todo(todo_id: int):
    """Delete one todo."""
    for index, todo in enumerate(todos):
        if todo.id == todo_id:
            todos.pop(index)
            return 204, ""
    return 404, {"error": "Todo not found", "todo_id": todo_id}


if __name__ == "__main__":
    app.run(port=8080, production=False)