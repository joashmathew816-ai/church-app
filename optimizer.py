from ortools.constraint_solver import pywrapcp, routing_enums_pb2
import requests
import time

TRAFFIC_MULTIPLIER = 1.2


# --------------------------
# GEOCODE
# --------------------------
def geocode(address):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}

    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "church-app"}
        )

        if response.status_code != 200:
            return None, f"❌ Server error for address: {address}"

        data = response.json()

        if not data:
            return None, f"❌ Invalid address: {address}"

        lat = data[0]["lat"]
        lon = data[0]["lon"]

        return f"{lon},{lat}", None

    except Exception:
        return None, f"❌ Could not process address: {address}"


# --------------------------
# OSRM
# --------------------------
def build_matrices(coords):
    url = (
        "http://router.project-osrm.org/table/v1/driving/"
        + ";".join(coords)
        + "?annotations=duration,distance"
    )

    data = requests.get(url).json()

    if "durations" not in data:
        raise Exception("OSRM error")

    return data["durations"], data["distances"]


# --------------------------
# GROUP + SPLIT
# --------------------------
def group_and_split(passengers, max_capacity):
    grouped = {}

    for p in passengers:
        grouped.setdefault(p["address"], []).append(p["name"])

    addresses, names = [], []

    for addr, people in grouped.items():
        for i in range(0, len(people), max_capacity):
            addresses.append(addr)
            names.append(people[i:i + max_capacity])

    return addresses, names


# --------------------------
# PARTIAL ASSIGNMENT
# --------------------------
def apply_partial_assignment(p_addresses, p_groups, capacities):
    total_capacity = sum(capacities)

    assigned_groups = []
    unassigned_groups = []

    current_load = 0

    for group in p_groups:
        if current_load + len(group) <= total_capacity:
            assigned_groups.append(group)
            current_load += len(group)
        else:
            unassigned_groups.append(group)

    assigned_addresses = p_addresses[:len(assigned_groups)]
    unassigned_names = [name for group in unassigned_groups for name in group]

    return assigned_addresses, assigned_groups, unassigned_names


# --------------------------
# MORNING
# --------------------------
def optimize_morning(drivers, passengers, church):

    if not drivers:
        return {"error": ["❌ No drivers selected"]}
    if not passengers:
        return {"error": ["❌ No passengers selected"]}

    driver_addresses = [d["address"] for d in drivers]
    capacities = [d["capacity"] for d in drivers]

    if not any(capacities):
        return {"error": ["❌ No driver has a capacity set"]}

    p_addresses, p_groups = group_and_split(passengers, max(capacities))

    p_addresses, p_groups, unassigned = apply_partial_assignment(
        p_addresses, p_groups, capacities
    )

    all_addresses = driver_addresses + p_addresses + [church]

    coords = []
    for addr in all_addresses:
        c, err = geocode(addr)
        if err:
            return {"error": [err]}
        coords.append(c)
        time.sleep(1)

    try:
        time_matrix, dist_matrix = build_matrices(coords)
    except:
        return {"error": ["❌ Routing failed — OSRM service unavailable"]}

    time_matrix = [[int(t * TRAFFIC_MULTIPLIER) for t in row] for row in time_matrix]

    church_index = len(all_addresses) - 1
    demands = [0] * len(driver_addresses) + [len(g) for g in p_groups] + [0]

    manager = pywrapcp.RoutingIndexManager(
        len(time_matrix),
        len(drivers),
        list(range(len(drivers))),
        [church_index] * len(drivers)
    )

    routing = pywrapcp.RoutingModel(manager)

    def cost(i, j):
        return int(time_matrix[manager.IndexToNode(i)][manager.IndexToNode(j)])

    routing.SetArcCostEvaluatorOfAllVehicles(
        routing.RegisterTransitCallback(cost)
    )

    def demand(i):
        return demands[manager.IndexToNode(i)]

    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(demand),
        0,
        capacities,
        True,
        "Capacity"
    )

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 20

    solution = routing.SolveWithParameters(params)

    if not solution:
        return {"error": ["❌ No solution found — try adding more drivers or reducing passengers"]}

    results = []
    total_time = 0
    total_distance = 0

    for v, driver in enumerate(drivers):
        index = routing.Start(v)
        route_time = 0
        route_dist = 0
        steps = []

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)

            if len(driver_addresses) <= node < church_index:
                steps.append({
                    "address": all_addresses[node],
                    "passengers": p_groups[node - len(driver_addresses)]
                })

            prev = index
            index = solution.Value(routing.NextVar(index))

            from_node = manager.IndexToNode(prev)
            to_node = manager.IndexToNode(index)

            route_time += time_matrix[from_node][to_node]
            route_dist += dist_matrix[from_node][to_node]

        total_time += route_time
        total_distance += route_dist

        results.append({
            "driver": driver["name"],
            "stops": steps,
            "time_min": int(route_time / 60),
            "distance_km": round(route_dist / 1000, 2)
        })

    return {
        "routes": results,
        "total_time_min": int(total_time / 60),
        "total_distance_km": round(total_distance / 1000, 2),
        "unassigned": unassigned
    }


# --------------------------
# RETURN
# --------------------------
def optimize_return(drivers, passengers, church):

    if not drivers:
        return {"error": ["❌ No drivers selected"]}
    if not passengers:
        return {"error": ["❌ No passengers selected"]}

    capacities = [d["capacity"] for d in drivers]

    if not any(capacities):
        return {"error": ["❌ No driver has a capacity set"]}

    p_addresses, p_groups = group_and_split(passengers, max(capacities))

    p_addresses, p_groups, unassigned = apply_partial_assignment(
        p_addresses, p_groups, capacities
    )

    all_addresses = [church] + p_addresses + [d["address"] for d in drivers]

    coords = []
    for addr in all_addresses:
        c, err = geocode(addr)
        if err:
            return {"error": [err]}
        coords.append(c)
        time.sleep(1)

    try:
        time_matrix, dist_matrix = build_matrices(coords)
    except:
        return {"error": ["❌ Routing failed — OSRM service unavailable"]}

    time_matrix = [[int(t * TRAFFIC_MULTIPLIER) for t in row] for row in time_matrix]

    church_index = 0
    passenger_start = 1
    driver_start = 1 + len(p_addresses)

    demands = [0] + [len(g) for g in p_groups] + [0] * len(drivers)

    starts = [church_index] * len(drivers)
    ends = list(range(driver_start, driver_start + len(drivers)))

    manager = pywrapcp.RoutingIndexManager(
        len(time_matrix),
        len(drivers),
        starts,
        ends
    )

    routing = pywrapcp.RoutingModel(manager)

    def cost(i, j):
        return int(time_matrix[manager.IndexToNode(i)][manager.IndexToNode(j)])

    routing.SetArcCostEvaluatorOfAllVehicles(
        routing.RegisterTransitCallback(cost)
    )

    def demand(i):
        return demands[manager.IndexToNode(i)]

    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(demand),
        0,
        capacities,
        True,
        "Capacity"
    )

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 20

    solution = routing.SolveWithParameters(params)

    if not solution:
        return {"error": ["❌ No return route found — try adding more drivers"]}

    results = []
    total_time = 0
    total_distance = 0

    for v, driver in enumerate(drivers):
        index = routing.Start(v)
        route_time = 0
        route_dist = 0
        steps = []

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)

            if passenger_start <= node < driver_start:
                steps.append({
                    "address": all_addresses[node],
                    "passengers": p_groups[node - passenger_start]
                })

            prev = index
            index = solution.Value(routing.NextVar(index))

            from_node = manager.IndexToNode(prev)
            to_node = manager.IndexToNode(index)

            route_time += time_matrix[from_node][to_node]
            route_dist += dist_matrix[from_node][to_node]

        total_time += route_time
        total_distance += route_dist

        results.append({
            "driver": driver["name"],
            "stops": steps,
            "time_min": int(route_time / 60),
            "distance_km": round(route_dist / 1000, 2)
        })

    return {
        "routes": results,
        "total_time_min": int(total_time / 60),
        "total_distance_km": round(total_distance / 1000, 2),
        "unassigned": unassigned
    }

def safe_optimize(func, drivers, passengers, destination):
    """
    Safely run an optimizer function and always return a dict.
    Never raises — always returns either results or an error dict.
    """
    try:
        result = func(drivers, passengers, destination)
        if result is None:
            return {"error": ["Optimizer returned no result."]}
        return result
    except Exception as e:
        return {"error": [f"Optimizer error: {str(e)}"]}