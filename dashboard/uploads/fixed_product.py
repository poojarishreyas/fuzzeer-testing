from flask import Flask, jsonify, request
import re

app = Flask(__name__)

# In-memory storage
products = {}
orders = {}
users = {}
next_product_id = 1
next_order_id = 1
next_user_id = 1

# ─────────────────────────────────────────────
# UTILITY FUNCTIONS FOR VALIDATION
# ─────────────────────────────────────────────

def is_valid_email(email):
    """Basic email format validation"""
    return re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email) is not None

# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

@app.route("/users", methods=["POST"])
def create_user():
    data = request.json
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400

    username = data.get("username")
    email = data.get("email")

    if not username:
        return jsonify({"error": "username is required"}), 400
    if not isinstance(username, str) or not username.strip():
        return jsonify({"error": "username must be a non-empty string"}), 400

    if not email:
        return jsonify({"error": "email is required"}), 400
    if not isinstance(email, str) or not is_valid_email(email): # Bug [1] fix for email type and format
        return jsonify({"error": "email must be a valid string email address"}), 422 # Use 422 for semantically invalid input

    # BUG: No duplicate user check -> Fix: Check for duplicate username or email
    for u in users.values():
        if u["username"] == username:
            return jsonify({"error": "Username already exists"}), 409
        if u["email"] == email:
            return jsonify({"error": "Email already exists"}), 409

    global next_user_id
    user = {
        "id": next_user_id,   # FIX: Caller cannot hijack ID
        "username": username,
        "email": email,
        "role": "user",       # FIX: Caller cannot set role=admin
        "balance": 0.0        # FIX: Caller cannot set their own balance, defaults to 0.0
    }
    users[user["id"]] = user
    next_user_id += 1
    return jsonify(user), 201


@app.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    user = users.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(user)


# ─────────────────────────────────────────────
# PRODUCTS
# ─────────────────────────────────────────────

@app.route("/products", methods=["POST"])
def create_product():
    global next_product_id
    data = request.json
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400

    name = data.get("name")
    price = data.get("price")
    stock = data.get("stock", 0) # Default stock if not provided
    category = data.get("category", "general")

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not isinstance(name, str) or not name.strip(): # Bug [2] fix for name type
        return jsonify({"error": "name must be a non-empty string"}), 422

    # BUG: No duplicate product name check -> Fix: Check for duplicate product name
    for p in products.values():
        if p["name"] == name and not p["deleted"]: # Check against non-deleted products
            return jsonify({"error": "Product with this name already exists"}), 409

    if price is None:
        return jsonify({"error": "price is required"}), 400
    if not isinstance(price, (int, float)): # Bug [2] fix for price type
        return jsonify({"error": "price must be a number"}), 422
    if price <= 0: # BUG: Accepts negative price -> Fix: price > 0 validation
        return jsonify({"error": "price must be greater than zero"}), 422

    if not isinstance(stock, int) or stock < 0: # Validate stock type and non-negativity
        return jsonify({"error": "stock must be a non-negative integer"}), 422

    if not isinstance(category, str) or not category.strip():
        return jsonify({"error": "category must be a non-empty string"}), 422

    product = {
        "id": next_product_id,  # FIX: ID cannot be hijacked
        "name": name,
        "price": float(price),  # Ensure price is stored as float
        "stock": stock,
        "category": category,
        "deleted": False
    }
    products[product["id"]] = product
    next_product_id += 1
    return jsonify(product), 201


@app.route("/products", methods=["GET"])
def list_products():
    result = []
    for p in products.values():
        if not p["deleted"]: # BUG: deleted products still show up -> Fix: Filter deleted products
            result.append({
                "id": p["id"],
                "name": p["name"],
                "price": p["price"],
                "stock": p["stock"],     # FIX: 'stock' missing from response
                "category": p["category"] # FIX: 'category' missing from response
            })
    return jsonify(result)


@app.route("/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    product = products.get(product_id)
    if not product or product["deleted"]: # FIX: Return 404 for deleted products
        return jsonify({"error": "Product not found"}), 404
    return jsonify(product)


@app.route("/products/<int:product_id>", methods=["PUT"])
def update_product(product_id):
    product = products.get(product_id)
    if not product or product["deleted"]: # BUG: Can update a deleted product -> Fix: check for deleted
        return jsonify({"error": "Product not found"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400

    updated_fields = {}

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            return jsonify({"error": "name must be a non-empty string"}), 422
        for pid, p in products.items():
            if pid != product_id and p["name"] == name and not p["deleted"]:
                return jsonify({"error": "Product with this name already exists"}), 409
        updated_fields["name"] = name

    if "price" in data:
        price = data["price"]
        if not isinstance(price, (int, float)):
            return jsonify({"error": "price must be a number"}), 422
        if price <= 0: # BUG: Accepts negative price -> Fix: price > 0 validation
            return jsonify({"error": "price must be greater than zero"}), 422
        updated_fields["price"] = float(price)

    if "stock" in data:
        stock = data["stock"]
        if not isinstance(stock, int) or stock < 0:
            return jsonify({"error": "stock must be a non-negative integer"}), 422
        updated_fields["stock"] = stock

    if "category" in data:
        category = data["category"]
        if not isinstance(category, str) or not category.strip():
            return jsonify({"error": "category must be a non-empty string"}), 422
        updated_fields["category"] = category

    # Do not allow updating 'id' or 'deleted' via PUT
    if "id" in data:
        return jsonify({"error": "ID cannot be updated"}), 400
    if "deleted" in data:
        return jsonify({"error": "Deleted status cannot be updated directly"}), 400

    if not updated_fields:
        return jsonify({"error": "No valid fields provided for update"}), 400

    products[product_id].update(updated_fields)
    return jsonify(products[product_id])


@app.route("/products/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    if product_id not in products:
        return jsonify({"error": "Product not found"}), 404
    
    # Bug [5]: Soft delete — The rule "After DELETE, return 404 if the resource is accessed again"
    # implies a hard delete or consistent filtering of soft-deleted items. Hard deleting for simplicity.
    del products[product_id] # FIX: Perform hard delete
    return "", 204 # FIX: Return 204 No Content for successful delete with no body


# ─────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────

@app.route("/orders", methods=["POST"])
def create_order():
    global next_order_id
    data = request.json
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400

    user_id = data.get("user_id")
    product_id = data.get("product_id")
    quantity = data.get("quantity") # Bug [3] fix: Check for quantity

    if user_id is None:
        return jsonify({"error": "user_id is required"}), 400
    if not isinstance(user_id, int) or user_id <= 0:
        return jsonify({"error": "user_id must be a positive integer"}), 422
    if user_id not in users:
        return jsonify({"error": "User not found"}), 404

    if product_id is None:
        return jsonify({"error": "product_id is required"}), 400
    if not isinstance(product_id, int) or product_id <= 0:
        return jsonify({"error": "product_id must be a positive integer"}), 422

    product = products.get(product_id)
    if not product or product["deleted"]: # FIX: Check if product exists and is not deleted
        return jsonify({"error": "Product not found"}), 404

    if quantity is None: # Bug [3] fix: Check for missing quantity
        return jsonify({"error": "quantity is required"}), 400
    if not isinstance(quantity, int) or quantity <= 0: # Bug [3] fix: Validate quantity type and value
        return jsonify({"error": "quantity must be a positive integer"}), 422

    if product["stock"] < quantity: # BUG: No stock check -> Fix: Check stock before order
        return jsonify({"error": "Not enough stock for product"}), 400

    total = product["price"] * quantity

    order = {
        "id": next_order_id,
        "user_id": user_id,
        "product_id": product_id,
        "quantity": quantity,
        "total": total,
        "status": "pending"  # BUG: accepts any status string -> Fix: Default to 'pending'
    }
    orders[next_order_id] = order
    next_order_id += 1

    products[product_id]["stock"] -= quantity # BUG: stock is never decremented -> Fix: Decrement stock
    return jsonify(order), 201


@app.route("/orders/<int:order_id>", methods=["GET"])
def get_order(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(order)


@app.route("/orders/<int:order_id>/status", methods=["PUT"])
def update_order_status(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400

    new_status = data.get("status")

    if not new_status:
        return jsonify({"error": "status is required"}), 400
    if not isinstance(new_status, str) or not new_status.strip():
        return jsonify({"error": "status must be a non-empty string"}), 422

    # Valid statuses and transitions
    valid_statuses = ["pending", "confirmed", "shipped", "delivered", "cancelled"]
    current_status = order["status"]

    if new_status not in valid_statuses: # BUG: Accepts any string as status -> Fix: Validate against enum
        return jsonify({"error": f"Invalid status: {new_status}. Allowed statuses are {', '.join(valid_statuses)}"}), 422

    # BUG: no validation of state transition (can go delivered → pending) -> Fix: Implement state transitions
    if current_status == "delivered":
        if new_status != "delivered":
            return jsonify({"error": "Cannot change status of a delivered order"}), 409 # Conflict
    elif current_status == "shipped":
        if new_status not in ["shipped", "delivered"]:
            return jsonify({"error": f"Cannot change status from 'shipped' to '{new_status}'"}), 409
    elif current_status == "confirmed":
        if new_status not in ["confirmed", "shipped", "cancelled"]:
            return jsonify({"error": f"Cannot change status from 'confirmed' to '{new_status}'"}), 409
    elif current_status == "pending":
        if new_status not in ["pending", "confirmed", "cancelled"]:
            return jsonify({"error": f"Cannot change status from 'pending' to '{new_status}'"}), 409
    elif current_status == "cancelled":
        if new_status != "cancelled":
            return jsonify({"error": "Cannot change status of a cancelled order"}), 409

    # If status is changing to cancelled, restock product (only if not already cancelled)
    if new_status == "cancelled" and current_status != "cancelled":
        product_to_restock = products.get(order["product_id"])
        if product_to_restock: # Check if product still exists (not hard-deleted)
            product_to_restock["stock"] += order["quantity"]
        order["status"] = new_status
        return jsonify(order)

    order["status"] = new_status
    return jsonify(order)


@app.route("/orders/<int:order_id>", methods=["DELETE"])
def cancel_order(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    
    # BUG: Chained bug — deleting order doesn't restock the product -> Fix: Restock product
    product_id = order["product_id"]
    quantity = order["quantity"]
    
    # Restock only if the product exists and the order was not already cancelled (or in a state not requiring restock)
    if product_id in products and order["status"] not in ["cancelled", "delivered"]:
        products[product_id]["stock"] += quantity
    
    del orders[order_id]
    return "", 204 # FIX: Return 204 No Content for successful delete with no body


# ─────────────────────────────────────────────
# RESTOCK
# ─────────────────────────────────────────────

@app.route("/products/<int:product_id>/restock", methods=["POST"])
def restock_product(product_id):
    product = products.get(product_id)
    if not product or product["deleted"]: # FIX: Return 404 for deleted products
        return jsonify({"error": "Product not found"}), 404
    
    data = request.json
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400

    quantity = data.get("quantity") # Bug [4] fix: Use .get()

    if quantity is None: # Bug [4] fix: Check for missing quantity
        return jsonify({"error": "quantity is required"}), 400
    if not isinstance(quantity, int):
        return jsonify({"error": "quantity must be an integer"}), 422
    if quantity <= 0: # BUG: Accepts 0 and negative quantities -> Fix: quantity > 0 validation
        return jsonify({"error": "quantity must be greater than zero"}), 422
    
    products[product_id]["stock"] += quantity
    return jsonify(products[product_id])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)