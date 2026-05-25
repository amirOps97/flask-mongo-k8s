import os
from flask import Flask, request, jsonify
from pymongo import MongoClient
from bson import ObjectId
from flasgger import Swagger

# SETUP

app = Flask(__name__)
swagger = Swagger(app)

mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/productdb")
client = MongoClient(mongo_uri)

db = client["productdb"]
products = db["products"]

# HELPER

def product_to_dict(product):
    return {
        "id": str(product["_id"]),
        "name": product["name"],
        "description": product["description"],
        "price": product["price"],
    }


# ROUTES (CRUD)

@app.route("/health", methods=["GET"])
def health():
    """Health Check
    ---
    responses:
      200:
        description: App is running
    """
    return jsonify({"status": "ok"})


@app.route("/api/products", methods=["POST"])
def create_product():
    """Create a product
    ---
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - name
            - description
            - price
          properties:
            name:
              type: string
              example: "Laptop"
            description:
              type: string
              example: "Gaming laptop 16GB RAM"
            price:
              type: number
              example: 999.99
    responses:
      201:
        description: Product created
      400:
        description: Missing required fields
    """
    data = request.get_json()

    if not data or not all(key in data for key in ["name", "description", "price"]):
        return jsonify({"error": "name, description, and price are required"}), 400

    product = {
        "name": data["name"],
        "description": data["description"],
        "price": data["price"],
    }

    result = products.insert_one(product)
    product["_id"] = result.inserted_id

    return jsonify(product_to_dict(product)), 201


@app.route("/api/products", methods=["GET"])
def get_all_products():
    """Get all products
    ---
    responses:
      200:
        description: List of all products
    """
    all_products = products.find()
    return jsonify([product_to_dict(p) for p in all_products])


@app.route("/api/products/<id>", methods=["GET"])
def get_product(id):
    """Get a product by ID
    ---
    parameters:
      - name: id
        in: path
        type: string
        required: true
        description: The product ID
    responses:
      200:
        description: Product found
      404:
        description: Product not found
    """
    try:
        product = products.find_one({"_id": ObjectId(id)})
    except Exception:
        return jsonify({"error": "invalid id"}), 400

    if not product:
        return jsonify({"error": "product not found"}), 404

    return jsonify(product_to_dict(product))


@app.route("/api/products/<id>", methods=["PUT"])
def update_product(id):
    """Update a product
    ---
    parameters:
      - name: id
        in: path
        type: string
        required: true
        description: The product ID
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            name:
              type: string
              example: "Updated Laptop"
            description:
              type: string
              example: "Updated description"
            price:
              type: number
              example: 799.99
    responses:
      200:
        description: Product updated
      404:
        description: Product not found
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "request body is required"}), 400

    try:
        result = products.update_one(
            {"_id": ObjectId(id)},
            {"$set": data}
        )
    except Exception:
        return jsonify({"error": "invalid id"}), 400

    if result.matched_count == 0:
        return jsonify({"error": "product not found"}), 404

    product = products.find_one({"_id": ObjectId(id)})
    return jsonify(product_to_dict(product))


@app.route("/api/products/<id>", methods=["DELETE"])
def delete_product(id):
    """Delete a product
    ---
    parameters:
      - name: id
        in: path
        type: string
        required: true
        description: The product ID
    responses:
      200:
        description: Product deleted
      404:
        description: Product not found
    """
    try:
        result = products.delete_one({"_id": ObjectId(id)})
    except Exception:
        return jsonify({"error": "invalid id"}), 400

    if result.deleted_count == 0:
        return jsonify({"error": "product not found"}), 404

    return jsonify({"message": "product deleted"})


# RUN

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)