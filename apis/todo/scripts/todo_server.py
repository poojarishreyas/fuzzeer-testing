from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

todos = {}
next_id = 1


def make_todo(id, title, description="", due_date=None, completed=False):
    return {
        "id": id,
        "title": title,
        "description": description,
        "completed": completed,
        "due_date": due_date,
        "created_at": datetime.utcnow().isoformat()
    }


@app.get("/todos")
def get_todos():
    return jsonify(list(todos.values()))


@app.get("/todo/<int:todo_id>")
def get_todo(todo_id):
    todo = todos.get(todo_id)
    if todo is None:
        return jsonify({"error": "Todo not found"}), 404
    return jsonify(todo)


@app.post("/todo")
def create_todo():
    global next_id
    title = request.args.get("title")
    description = request.args.get("description", "")
    due_date = request.args.get("due_date")
    if not title:
        return jsonify({"error": "title is required"}), 400
    todo = make_todo(next_id, title, description, due_date)
    todos[next_id] = todo
    next_id += 1
    return jsonify(todo), 201


@app.put("/todo")
def update_todo():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body required"}), 400
    todo_id = data.get("id")
    if todo_id is None or todo_id not in todos:
        return jsonify({"error": "Todo not found"}), 404
    todo = todos[todo_id]
    todo["title"] = data.get("title", todo["title"])
    todo["description"] = data.get("description", todo["description"])
    todo["completed"] = data.get("completed", todo["completed"])
    todo["due_date"] = data.get("due_date", todo["due_date"])
    return jsonify(todo)


@app.delete("/todo")
def delete_todo():
    todo_id = request.args.get("id", type=int)
    if todo_id is None or todo_id not in todos:
        return jsonify({"error": "Todo not found"}), 404
    del todos[todo_id]
    return jsonify({"message": "Todo deleted successfully"})


@app.patch("/todo/<int:todo_id>/complete")
def complete_todo(todo_id):
    todo = todos.get(todo_id)
    if todo is None:
        return jsonify({"error": "Todo not found"}), 404
    todo["completed"] = True
    return jsonify(todo)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)
