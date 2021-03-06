"""Order Verifier Component

Upon receiving a Pizza Order, this component validates the order and checks the store's stock.
If sufficient stock exists, Order Verifier decrements the store's stock and creates the order. 
Otherwise, Order Verifier requests a restock before decrementing stock and creating the order.
"""

import copy
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

import jsonschema
import requests
from cassandra.cluster import Cluster
from flask import Flask, Response, request

from flask import Flask, request, Response
from cassandra.cluster import Cluster
import jsonschema

__author__ = "Chris Scott"
__version__ = "2.0.0"
__maintainer__ = "Chris Scott"
__email__ = "cms190009@utdallas.edu"
__status__ = "Development"

# Connect to Cassandra service
cass_IP = os.environ["CASS_DB"]
cluster = Cluster([cass_IP])
session = cluster.connect('pizza_grocery')

# Cassandra prepared statements
select_stock_prepared = session.prepare('SELECT * FROM stock WHERE storeID=?')
select_items_prepared = session.prepare('SELECT * FROM items WHERE name=?')
update_stock_prepared = session.prepare('\
    UPDATE stock \
    SET quantity=? \
    WHERE storeID=? AND itemName=?\
')
insert_customers_prepared = session.prepare('\
    INSERT INTO customers (customerName, latitude, longitude) \
    VALUES (?, ?, ?)\
')
insert_payments_prepared = session.prepare('\
    INSERT INTO payments (paymentToken, method) \
    VALUES (?, ?)\
')
insert_pizzas_prepared = session.prepare('\
    INSERT INTO pizzas (pizzaID, toppings, cost) \
    VALUES (?, ?, ?)\
')
insert_order_prepared = session.prepare('\
    INSERT INTO orderTable \
        (orderID, orderedFrom, orderedBy, deliveredBy, containsPizzas, \
            containsItems, paymentID, placedAt, active, estimatedDeliveryTime) \
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\
')
insert_order_by_store_prepared = session.prepare('\
    INSERT INTO orderByStore (orderedFrom, placedAt, orderID) \
    VALUES (?, ?, ?)\
')
insert_order_by_customer_prepared = session.prepare('\
    INSERT INTO orderByCustomer (orderedBy, placedAt, orderID) \
    VALUES (?, ?, ?)\
')

# Create Flask app
app = Flask(__name__)

# Open jsonschema for pizza-order
with open("src/pizza-order.schema.json", "r") as pizza_schema:
    pizza_schema = json.loads(pizza_schema.read())

# Open jsonschema for workflow-request
with open("src/workflow-request.schema.json", "r") as workflow_schema:
    workflow_schema = json.loads(workflow_schema.read())

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('requests').setLevel(logging.INFO)

# Global pizza items/ingredients dict
items_dict = {
    'Dough': 0,         'SpicySauce': 0,        'TraditionalSauce': 0,  'Cheese': 0,
    'Pepperoni': 0,     'Sausage': 0,           'Beef': 0,              'Onion': 0,
    'Chicken': 0,       'Peppers': 0,           'Olives': 0,            'Bacon': 0,
    'Pineapple': 0,     'Mushrooms': 0
}

# Global workflows dict
workflows = dict()


# Calculate pizza price based on ingredients
def calc_pizza_cost(ingredient_set):
    cost = 0.0
    for ingredient in ingredient_set:
        result = session.execute(select_items_prepared, (ingredient[0],))
        for (name, price) in result:
            cost += price * ingredient[1] 
    return cost


# Insert an order's pizza(s) into 'pizzas' table
def insert_pizzas(pizza_list):
    pizza_uuid_set = set()

    for pizza in pizza_list:
        pizza_uuid = uuid.uuid4()
        pizza_uuid_set.add(pizza_uuid)
        ingredient_set = set()

        if pizza["crustType"] == "Thin":
            ingredient_set.add(("Dough", 1))
        elif pizza["crustType"] == "Traditional":
            ingredient_set.add(("Dough", 2))

        if pizza["sauceType"] == "Spicy":
            ingredient_set.add(("SpicySauce", 1))
        elif pizza["sauceType"] == "Traditional":
            ingredient_set.add(("TraditionalSauce", 1))

        if pizza["cheeseAmt"] == "Light":
            ingredient_set.add(("Cheese", 1))
        elif pizza["cheeseAmt"] == "Normal":
            ingredient_set.add(("Cheese", 2))
        elif pizza["cheeseAmt"] == "Extra":
            ingredient_set.add(("Cheese", 3))

        for topping in pizza["toppingList"]:
            ingredient_set.add((topping, 1))
        
        cost = calc_pizza_cost(ingredient_set)
        session.execute(insert_pizzas_prepared, (pizza_uuid, ingredient_set, cost))
    
    return pizza_uuid_set


# Insert order info into DB
def create_order(order_dict):
    order_uuid = uuid.UUID(order_dict["orderId"])
    store_uuid = uuid.UUID(order_dict["storeId"])
    pay_uuid = uuid.UUID(order_dict["paymentToken"])
    cust_name = order_dict["custName"]
    cust_lat = order_dict["custLocation"]["lat"]
    cust_lon = order_dict["custLocation"]["lon"]
    placed_at = datetime.strptime(order_dict["orderDate"], '%Y-%m-%dT%H:%M:%S')

    # Insert customer information into 'customers' table
    session.execute(insert_customers_prepared, (cust_name, cust_lat, cust_lon))
    # Insert order payment information into 'payments' table
    session.execute(insert_payments_prepared, (pay_uuid, order_dict["paymentTokenType"]))  
    # Insert the ordered pizzas into 'pizzas' table
    pizza_uuid_set = insert_pizzas(order_dict["pizzaList"])
    # Insert order into 'orderTable' table
    session.execute(
        insert_order_prepared, 
        (order_uuid, store_uuid, cust_name, "", pizza_uuid_set, None, pay_uuid, placed_at, True, -1)
    )
    # Insert order into 'orderByStore' table
    session.execute(insert_order_by_store_prepared, (store_uuid, placed_at, order_uuid))
    # Insert order into 'orderByCustomer' table
    session.execute(insert_order_by_customer_prepared, (cust_name, placed_at, order_uuid))


# Decrement a store's stock for the order about to be placed
def decrement_stock(store_uuid, in_stock_dict, req_item_dict):
    for item_name in req_item_dict:
        quantity = in_stock_dict[item_name] - req_item_dict[item_name]
        session.execute(update_stock_prepared, (quantity, store_uuid, item_name))


# Aggregate all ingredients for a given order
def aggregate_ingredients(pizza_list):
    ingredients = copy.deepcopy(items_dict)

    # Loop through each pizza in pizza_list and aggregate the required ingredients
    for pizza in pizza_list:
        if pizza['crustType'] == 'Thin':
            ingredients['Dough'] += 1
        elif pizza['crustType'] == 'Traditional':
            ingredients['Dough'] += 2

        if pizza['sauceType'] == 'Spicy':
            ingredients['SpicySauce'] += 1
        elif pizza['sauceType'] == 'Traditional':
            ingredients['TraditionalSauce'] += 1

        if pizza['cheeseAmt'] == 'Light':
            ingredients['Cheese'] += 1
        elif pizza['cheeseAmt'] == 'Normal':
            ingredients['Cheese'] += 2
        elif pizza['cheeseAmt'] == 'Extra':
            ingredients['Cheese'] += 3

        for topping in pizza["toppingList"]:
            ingredients[topping] += 1

    return ingredients


# Check stock at a given store to determine if order can be filled
def check_stock(order_dict):
    in_stock_dict = copy.deepcopy(items_dict)
    store_id = uuid.UUID(order_dict["storeId"])
    req_item_dict = aggregate_ingredients(order_dict["pizzaList"])
    restock_list = list()

    # If insufficient stock, restock_list contains items for restock
    # Otherwise, restock_list is an empty list
    rows = session.execute(select_stock_prepared, (store_id,))
    for row in rows:
        if row.quantity < req_item_dict[row.itemname]:
            restock_list.append({"item-name": row.itemname, "quantity": req_item_dict[row.itemname]})
        in_stock_dict[row.itemname] = row.quantity

    return in_stock_dict, req_item_dict, restock_list


# Manages order creation and restock, if needed
def order_manager(order_dict):
    store_id = order_dict["storeId"]
    if not (store_id in workflows):
        logging.info("Request is valid, but workflow does not exist.")
        return Response(status=422, response="Order request is valid, but workflow does not exist.")

    order_dict["orderId"] = str(uuid.uuid4())  # Assign order_id to order_dict
    order_id = order_dict["orderId"]
    logging.info("Order assigned ID " + order_id)

    # Check stock to see if order can be placed. If not, restock and check again
    while True:
        in_stock_dict, req_item_dict, restock_list = check_stock(order_dict)
        if restock_list:
            if "restocker" in workflows[store_id]["component-list"]:    
                restock_dict = {"storeID": store_id, "restock-list": restock_list}
                response = requests.post("http://restocker:5000/restock", json=json.dumps(restock_dict))
                logging.info("Restocker - {}, {}".format(response.status_code, response.text))
                if response.status_code != 200:
                    # Restock unsuccesful, must reject order request
                    return Response(status=424, response="{}, {}".format(response.status_code, response.text))
            else:
                logging.info("{} rejected, insufficient stock at {}".format(order_id, store_id))
                return Response(status=404, response="Insufficient stock at {}\n".format(store_id))
        else:
            break

    # Decrement stock and create the order
    decrement_stock(uuid.UUID(store_id), in_stock_dict, req_item_dict)
    create_order(order_dict)

    # TODO: Send pizza order to auto-restocker
    if "auto-restocker" in workflows[store_id]["component-list"]:
        response = requests.post("http://restocker:4000/order", json=json.dumps(order_dict))
        logging.info("Auto-Restocker - {}, {}".format(response.status_code, response.text))

    # TODO: Assign delivery entity
    if "delivery-assigner" in workflows[store_id]["component-list"]:
        response = requests.post("http://delivery-assigner:3000/assign-entity", json=json.dumps(order_dict))
        logging.info("Delivery Assigner - {}, {}".format(response.status_code, response.text))
        if response.status_code != 200:
            # Could not assign delivery entity, but order has been created
            logging.info("Order {} created, but failed to assign delivery entity".format(order_id))
            return Response(status=response.status_code, response=response.text)

    return Response(status=201, response="Order {} has been accepted".format(order_id))


# validate pizza-order against schema
def verify_order(data):
    global pizza_schema
    valid = True
    mess = None
    try:
        jsonschema.validate(instance=data, schema=pizza_schema)
    except Exception as inst:
        valid = False
        mess = inst.args[0]
    return valid, mess


# if pizza-order is valid, try to create it
@app.route('/order', methods=['POST'])
def order_funct():
    data = json.loads(request.get_json())
    valid, mess = verify_order(data)
    if not valid:
        logging.info("pizza-order request ill formatted")
        return Response(status=400, response="pizza-order request ill formatted\n" + mess)
    return order_manager(data)


# validate workflow-request against schema
def verify_workflow(data):
    global workflow_schema
    valid = True
    mess = None
    try:
        jsonschema.validate(instance=data, schema=workflow_schema)
    except Exception as inst:
        valid = False
        mess = inst.args[0]
    return valid, mess


# if workflow-request is valid and does not exist, create it
@app.route("/workflow-requests/<storeId>", methods=['PUT'])
def setup_workflow(storeId):
    logging.info("PUT /workflow-requests/" + storeId)
    data = json.loads(request.get_json())
    valid, mess = verify_workflow(data)

    if not valid:
        logging.info("workflow-request ill formatted")
        return Response(status=400, response="workflow-request ill formatted\n" + mess)

    if storeId in workflows:
        logging.info("Workflow " + storeId + " already exists")
        return Response(status=409, response="Workflow " + storeId + " already exists\n")
    
    if not ("cass" in data["component-list"]):
        logging.info("workflow-request rejected, cass is a required workflow component")
        return Response(status=422, response="workflow-request rejected, cass is a required workflow component\n")

    workflows[storeId] = data

    logging.info("Workflow started for {}\n".format(storeId))
    
    return Response(status=201, response="Order Verifier deployed for {}\n".format(storeId))    


# if the recource exists, update it
@app.route("/workflow-update/<storeId>", methods=['PUT'])
def update_workflow(storeId):
    logging.info("PUT /workflow-update/" + storeId)
    data = json.loads(request.get_json())
    valid, mess = verify_workflow(data)

    if not valid:
        logging.info("workflow-request ill formatted")
        return Response(status=400, response="workflow-request ill formatted\n" + mess)

    if not ("cass" in data["component-list"]):
        logging.info("workflow-request rejected, cass is a required workflow component")
        return Response(status=422, response="workflow-request rejected, cass is a required workflow component\n")

    workflows[storeId] = data

    logging.info("Workflow updated for {}\n".format(storeId))

    return Response(status=200, response="Order Verifier updated for {}\n".format(storeId))


# if the recource exists, remove it
@app.route("/workflow-requests/<storeId>", methods=["DELETE"])
def teardown_workflow(storeId):
    logging.info("DELETE /workflow-requests/" + storeId)
    if not (storeId in workflows):
        return Response(status=404, response="Workflow doesn't exist. Nothing to teardown.\n")
    else:
        del workflows[storeId]
        return Response(status=204)


# retrieve the specified resource, if it exists
@app.route("/workflow-requests/<storeId>", methods=["GET"])
def retrieve_workflow(storeId):
    logging.info("GET /workflow-requests/" + storeId)
    if not (storeId in workflows):
        return Response(status=404, response="Workflow doesn't exist. Nothing to retrieve.\n")
    else:
        return Response(status=200, response=json.dumps(workflows[storeId]))


# retrieve all resources
@app.route("/workflow-requests", methods=["GET"])
def retrieve_workflows():
    logging.info("GET /workflow-requests")
    return Response(status=200, response=json.dumps(workflows))


# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    logging.info("GET /health")
    return Response(status=200,response="healthy\n")
