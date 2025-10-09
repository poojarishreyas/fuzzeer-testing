from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory storage
products = {}
orders = {}
users = {}
next_product_id = 1
next_order_id = 1
next_user_id = 1

# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

# BUG: Mass Assignment — accepts 'role' and 'id' from request body
# BUG: No duplicate user check
@app.route("/users", methods=["POST"])
def create_user():
    data = request.json
    global next_user_id
    if not data or "username" not in data or "email" not in data:
        return jsonify({"error": "username and email are required"}), 400
    user = {
        "id": data.get("id", next_user_id),   # BUG: caller can hijack ID
        "username": data["username"],
        "email": data["email"],
        "role": data.get("role", "user"),      # BUG: caller can set role=admin
        "balance": data.get("balance", 0.0)   # BUG: caller can set their own balance
    }
    users[user["id"]] = user
    next_user_id += 1
    return jsonify(user), 201


# BUG: No auth check — anyone can see any user
# BUG: Returns password_hash if it exists (contract violation)
@app.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    user = users.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(user)  # BUG: leaks role, balance, sensitive fields


# ─────────────────────────────────────────────
# PRODUCTS
# ─────────────────────────────────────────────

# BUG: Accepts negative price (business logic)
# BUG: Accepts 'id' in body (mass assignment)
# BUG: No duplicate product name check (CRUD)
@app.route("/products", methods=["POST"])
def create_product():
    global next_product_id
    data = request.json
    if not data or "name" not in data or "price" not in data:
        return jsonify({"error": "name and price are required"}), 400
    product = {
        "id": data.get("id", next_product_id),  # BUG: ID hijacking
        "name": data["name"],
        "price": data["price"],                  # BUG: no price > 0 validation
        "stock": data.get("stock", 0),
        "category": data.get("category", "general"),
        "deleted": False
    }
    products[product["id"]] = product
    next_product_id += 1
    return jsonify(product), 201


# BUG: Returns deleted products (ghost data)
# BUG: Missing 'category' field sometimes (contract violation)
@app.route("/products", methods=["GET"])
def list_products():
    result = []
    for p in products.values():
        # BUG: deleted products still show up
        result.append({
            "id": p["id"],
            "name": p["name"],
            "price": p["price"]
            # BUG: 'stock' and 'category' missing from response
        })
    return jsonify(result)


@app.route("/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    product = products.get(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404
    return jsonify(product)


# BUG: Can update a deleted product (ghost update / chained bug)
# BUG: Accepts negative price (business logic)
@app.route("/products/<int:product_id>", methods=["PUT"])
def update_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    data = request.json
    # BUG: no check if product is deleted before updating
    products[product_id].update({k: v for k, v in data.items() if k != "id"})
    return jsonify(products[product_id])


# BUG: Soft delete — marks deleted=True but doesn't remove
# This causes ghost data in list and ghost updates above
@app.route("/products/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    products[product_id]["deleted"] = True   # BUG: soft delete, still accessible
    return jsonify({"message": "Deleted"})


# ─────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────

# BUG: Crash on null quantity (crash detection)
# BUG: No stock check before placing order (business logic)
# BUG: Accepts invalid status in body (business logic)
@app.route("/orders", methods=["POST"])
def create_order():
    global next_order_id
    data = request.json
    if not data or "product_id" not in data or "user_id" not in data:
        return jsonify({"error": "product_id and user_id are required"}), 400

    product = products.get(data["product_id"])
    if not product:
        return jsonify({"error": "Product not found"}), 404

    quantity = data["quantity"]          # BUG: crashes if quantity is null/missing
    total = product["price"] * quantity  # BUG: no check product is deleted

    order = {
        "id": next_order_id,
        "user_id": data["user_id"],
        "product_id": data["product_id"],
        "quantity": quantity,
        "total": total,
        "status": data.get("status", "pending")  # BUG: accepts any status string
    }
    orders[next_order_id] = order
    next_order_id += 1
    # BUG: stock is never decremented
    return jsonify(order), 201


@app.route("/orders/<int:order_id>", methods=["GET"])
def get_order(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(order)


# BUG: Accepts any string as status (invalid state transition)
# Valid: pending → confirmed → shipped → delivered
@app.route("/orders/<int:order_id>/status", methods=["PUT"])
def update_order_status(order_id):
    if order_id not in orders:
        return jsonify({"error": "Order not found"}), 404
    data = request.json
    # BUG: no validation of allowed statuses
    # BUG: no validation of state transition (can go delivered → pending)
    orders[order_id]["status"] = data.get("status")
    return jsonify(orders[order_id])


# BUG: Chained bug — deleting order doesn't restock the product
@app.route("/orders/<int:order_id>", methods=["DELETE"])
def cancel_order(order_id):
    if order_id not in orders:
        return jsonify({"error": "Order not found"}), 404
    del orders[order_id]
    # BUG: product stock not restored after cancellation
    return jsonify({"message": "Order cancelled"})


# ─────────────────────────────────────────────
# RESTOCK
# ─────────────────────────────────────────────

# BUG: Crashes on missing/null quantity (crash detection)
# BUG: Accepts 0 and negative quantities (business logic)
@app.route("/products/<int:product_id>/restock", methods=["POST"])
def restock_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    data = request.json
    qty = data["quantity"]             # BUG: KeyError crash if quantity missing
    products[product_id]["stock"] += qty  # BUG: no validation qty > 0
    return jsonify(products[product_id])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)
