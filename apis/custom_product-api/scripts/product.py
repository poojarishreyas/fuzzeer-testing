from flask import Flask, jsonify, request

app = Flask(__name__)

products = {}
next_id = 1

@app.route("/products", methods=["GET"])
def list_products():
    category = request.args.get("category")
    result = list(products.values())
    if category:
        result = [p for p in result if p["category"] == category]
    return jsonify(result)

@app.route("/product/<int:product_id>", methods=["GET"])
def get_product(product_id):
    product = products.get(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404
    return jsonify(product)

@app.route("/product", methods=["POST"])
def create_product():
    global next_id
    data = request.json
    if not data or "name" not in data or "price" not in data:
        return jsonify({"error": "name and price are required"}), 400
    product = {
        "id": next_id,
        "name": data["name"],
        "price": data["price"],
        "category": data.get("category", "general"),
        "stock": data.get("stock", 0)
    }
    products[next_id] = product
    next_id += 1
    return jsonify(product), 201

@app.route("/product/<int:product_id>", methods=["PUT"])
def update_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    data = request.json
    products[product_id].update({k: v for k, v in data.items() if k != "id"})
    return jsonify(products[product_id])

@app.route("/product/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    del products[product_id]
    return jsonify({"message": "Deleted successfully"})

@app.route("/product/<int:product_id>/restock", methods=["POST"])
def restock_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    qty = request.json.get("quantity", 0)
    if qty <= 0:
        return jsonify({"error": "quantity must be positive"}), 400
    products[product_id]["stock"] += qty
    return jsonify(products[product_id])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)
