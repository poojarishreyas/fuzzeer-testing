from flask import Flask, request, jsonify

app = Flask(__name__)

books = {}
next_id = 1


def book_to_read(b):
    return {
        "id": b["id"],
        "title": b["title"],
        "author": b["author"],
        "price": b["price"],
        "copiesSold": b.get("copiesSold", 0),
    }


@app.get("/books")
def get_books():
    return jsonify([book_to_read(b) for b in books.values()])


@app.get("/book/<int:book_id>")
def get_book(book_id):
    book = books.get(book_id)
    if book is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(book_to_read(book))


@app.post("/book")
def create_book():
    global next_id
    title = request.args.get("title")
    author = request.args.get("author")
    price = request.args.get("price")
    if not title or not author or price is None:
        return jsonify({"error": "Missing required parameters: title, author, price"}), 400
    try:
        price = float(price)
    except ValueError:
        return jsonify({"error": "price must be a number"}), 400
    book = {"id": next_id, "title": title, "author": author, "price": price, "copiesSold": 0}
    books[next_id] = book
    next_id += 1
    return jsonify(book_to_read(book))


@app.put("/book")
def update_book():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body required"}), 400
    book_id = data.get("id")
    if book_id is None or book_id not in books:
        return jsonify({"error": "Book not found"}), 404
    book = books[book_id]
    book["title"] = data.get("title", book["title"])
    book["author"] = data.get("author", book["author"])
    book["price"] = data.get("price", book["price"])
    return jsonify({"id": book["id"], "title": book["title"], "author": book["author"], "price": book["price"]})


@app.delete("/book")
def delete_book():
    book_id = request.args.get("id", type=int)
    if book_id is None or book_id not in books:
        return jsonify({"error": "Book not found"}), 404
    del books[book_id]
    return jsonify({"message": "Book deleted successfully"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
