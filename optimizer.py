from ortools.constraint_solver import pywrapcp, routing_enums_pb2
import requests
import time

TRAFFIC_MULTIPLIER = 1.2


# --------------------------
# GEOCODE — with fallbacks
# --------------------------
def geocode(address):
    """
    Try multiple strategies to geocode an address.
    Returns (coord_string, None) on success or (None, error_message) on failure.
    """
    headers = {"User-Agent": "church-app"}
    url     = "https://nominatim.openstreetmap.org/search"

    # Build a list of progressively simpler versions of the address to try
    strategies = [address]

    parts = [p.strip() for p in address.split(",")]

    # Try first 3 parts: street, city, province
    if len(parts) >= 3:
        shorter = ", ".join(parts[:3])
        if shorter != address:
            strategies.append(shorter)

    # Try first 2 parts: street, city
    if len(parts) >= 2:
        shortest = ", ".join(parts[:2])
        if shortest not in strategies:
            strategies.append(shortest)

    # Try just the city (second part) on its own
    if len(parts) >= 2:
        city_only = parts[1]
        if city_only not in strategies:
            strategies.append(city_only)

    last_error = f"Could not geocode: {address}"

    for attempt in strategies:
        try:
            time.sleep(0.5)  # respect Nominatim rate limit
            response = requests.get(
                url,
                params  = {"q": attempt, "format": "json", "limit": 1},
                headers = headers,
                timeout = 10
            )

            if response.status_code != 200:
                last_error = (f"Server error {response.status_code} "
                              f"for: {attempt}")
                continue

            data = response.json()

            if not data:
                last_error = f"No results for: {attempt}"
                continue

            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            return f"{lon},{lat}", None

        except Exception as e:
            last_error = f"Error geocoding {attempt}: {str(e)}"
            continue

    return None, (
        f"Could not find address on map: {address}. "
        f"Please ask the user to update their address in their profile "
        f"by selecting a more specific option from the dropdown."
    )


# --------------------------
# OSRM — with fallback
# --------------------------
def build_matrices(coords):
    """
    Build time and distance matrices using OSRM.
    Falls back to straight-line estimates if OSRM fails.
    """
    url = (
        "http://router.project-osrm.org/table/v1/driving/"
        + ";".join(coords)
        + "?annotations=duration,distance"
    )

    try:
        response = requests.get(url, timeout=30)
        data     = response.json()

        if "durations" not in data or "distances" not in data:
            raise Exception("OSRM returned no data")

        # Check for None values in matrix — replace with fallback
        durations  = data["durations"]
        distances  = data["distances"]
        n          = len(coords)

        for i in range(n):
            for j in range(n):
                if durations[i][j] is None:
                    durations[i][j] = haversine_time(coords[i], coords[j])
                if distances[i][j] is None:
                    distances[i][j] = haversine_distance(coords[i], coords[j])

        return durations, distances

    except Exception as e:
        # Full fallback — build matrices from straight-line distances
        n         = len(coords)
        durations = []
        distances = []

        for i in range(n):
            dur_row = []
            dist_row = []
            for j in range(n):
                dur_row.append(haversine_time(coords[i], coords[j]))
                dist_row.append(haversine_distance(coords[i], coords[j]))
            durations.append(dur_row)
            distances.append(dist_row)

        return durations, distances


def parse_coord(coord_string):
    """Parse 'lon,lat' string into (lat, lon) floats."""
    parts = coord_string.split(",")
    return float(parts[1]), float(parts[0])  # returns (lat, lon)


def haversine_distance(coord1_str, coord2_str):
    """
    Calculate straight-line distance in metres between two
    'lon,lat' coordinate strings.
    """
    import math
    lat1, lon1 = parse_coord(coord1_str)
    lat2, lon2 = parse_coord(coord2_str)

    R    = 6371000  # Earth radius in metres
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def haversine_time(coord1_str, coord2_str):
    """
    Estimate driving time in seconds from straight-line distance.
    Assumes 40 km/h average city speed.
    """
    dist_m  = haversine_distance(coord1_str, coord2_str)
    speed   = 40 / 3.6  # 40 km/h in m/s
    return dist_m / speed if speed > 0 else 0


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

    assigned_groups   = []
    unassigned_groups = []
    current_load      = 0

    for group in p_groups:
        if current_load + len(group) <= total_capacity:
            assigned_groups.append(group)
            current_load += len(group)
        else:
            unassigned_groups.append(group)

    assigned_addresses = p_addresses[:len(assigned_groups)]
    unassigned_names   = [n for g in unassigned_groups for n in g]

    return assigned_addresses, assigned_groups, unassigned_names


# --------------------------
# MORNING
# --------------------------
def optimize_morning(drivers, passengers, church):

    if not drivers:
        return {"error": ["No drivers selected"]}
    if not passengers:
        return {"error": ["No passengers selected"]}

    driver_addresses = [d["address"] for d in drivers]
    capacities       = [d["capacity"] for d in drivers]

    if not any(capacities):
        return {"error": ["No driver has a capacity set"]}

    p_addresses, p_groups = group_and_split(passengers, max(capacities))
    p_addresses, p_groups, unassigned = apply_partial_assignment(
        p_addresses, p_groups, capacities
    )

    all_addresses = driver_addresses + p_addresses + [church]

    # Geocode all addresses
    coords = []
    for addr in all_addresses:
        c, err = geocode(addr)
        if err:
            return {"error": [err]}
        coords.append(c)

    try:
        time_matrix, dist_matrix = build_matrices(coords)
    except Exception as e:
        return {"error": [f"Routing failed: {str(e)}"]}

    time_matrix = [
        [int(t * TRAFFIC_MULTIPLIER) for t in row]
        for row in time_matrix
    ]

    church_index = len(all_addresses) - 1
    demands      = (
        [0] * len(driver_addresses)
        + [len(g) for g in p_groups]
        + [0]
    )

    try:
        manager = pywrapcp.RoutingIndexManager(
            len(time_matrix),
            len(drivers),
            list(range(len(drivers))),
            [church_index] * len(drivers)
        )

        routing = pywrapcp.RoutingModel(manager)

        transit_cb = routing.RegisterTransitCallback(
            lambda i, j: int(
                time_matrix[manager.IndexToNode(i)][manager.IndexToNode(j)]
            )
        )
        routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

        demand_cb = routing.RegisterUnaryTransitCallback(
            lambda i: demands[manager.IndexToNode(i)]
        )
        routing.AddDimensionWithVehicleCapacity(
            demand_cb, 0, capacities, True, "Capacity"
        )

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
        params.time_limit.seconds = 20

        solution = routing.SolveWithParameters(params)

    except Exception as e:
        return {"error": [f"Optimizer error: {str(e)}"]}

    if not solution:
        return {"error": [
            "No solution found — try adding more drivers "
            "or reducing the number of passengers."
        ]}

    results        = []
    total_time     = 0
    total_distance = 0

    for v, driver in enumerate(drivers):
        index      = routing.Start(v)
        route_time = 0
        route_dist = 0
        steps      = []

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)

            if len(driver_addresses) <= node < church_index:
                steps.append({
                    "address":    all_addresses[node],
                    "passengers": p_groups[node - len(driver_addresses)]
                })

            prev      = index
            index     = solution.Value(routing.NextVar(index))
            from_node = manager.IndexToNode(prev)
            to_node   = manager.IndexToNode(index)

            route_time += time_matrix[from_node][to_node]
            route_dist += dist_matrix[from_node][to_node]

        total_time     += route_time
        total_distance += route_dist

        results.append({
            "driver":      driver["name"],
            "stops":       steps,
            "time_min":    int(route_time / 60),
            "distance_km": round(route_dist / 1000, 2)
        })

    return {
        "routes":             results,
        "total_time_min":     int(total_time / 60),
        "total_distance_km":  round(total_distance / 1000, 2),
        "unassigned":         unassigned
    }


# --------------------------
# RETURN
# --------------------------
def optimize_return(drivers, passengers, church):

    if not drivers:
        return {"error": ["No drivers selected"]}
    if not passengers:
        return {"error": ["No passengers selected"]}

    capacities = [d["capacity"] for d in drivers]

    if not any(capacities):
        return {"error": ["No driver has a capacity set"]}

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

    try:
        time_matrix, dist_matrix = build_matrices(coords)
    except Exception as e:
        return {"error": [f"Routing failed: {str(e)}"]}

    time_matrix = [
        [int(t * TRAFFIC_MULTIPLIER) for t in row]
        for row in time_matrix
    ]

    church_index    = 0
    passenger_start = 1
    driver_start    = 1 + len(p_addresses)

    demands = (
        [0]
        + [len(g) for g in p_groups]
        + [0] * len(drivers)
    )

    starts = [church_index] * len(drivers)
    ends   = list(range(driver_start, driver_start + len(drivers)))

    try:
        manager = pywrapcp.RoutingIndexManager(
            len(time_matrix),
            len(drivers),
            starts,
            ends
        )

        routing = pywrapcp.RoutingModel(manager)

        transit_cb = routing.RegisterTransitCallback(
            lambda i, j: int(
                time_matrix[manager.IndexToNode(i)][manager.IndexToNode(j)]
            )
        )
        routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

        demand_cb = routing.RegisterUnaryTransitCallback(
            lambda i: demands[manager.IndexToNode(i)]
        )
        routing.AddDimensionWithVehicleCapacity(
            demand_cb, 0, capacities, True, "Capacity"
        )

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
        params.time_limit.seconds = 20

        solution = routing.SolveWithParameters(params)

    except Exception as e:
        return {"error": [f"Optimizer error: {str(e)}"]}

    if not solution:
        return {"error": [
            "No return route found — try adding more drivers."
        ]}

    results        = []
    total_time     = 0
    total_distance = 0

    for v, driver in enumerate(drivers):
        index      = routing.Start(v)
        route_time = 0
        route_dist = 0
        steps      = []

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)

            if passenger_start <= node < driver_start:
                steps.append({
                    "address":    all_addresses[node],
                    "passengers": p_groups[node - passenger_start]
                })

            prev      = index
            index     = solution.Value(routing.NextVar(index))
            from_node = manager.IndexToNode(prev)
            to_node   = manager.IndexToNode(index)

            route_time += time_matrix[from_node][to_node]
            route_dist += dist_matrix[from_node][to_node]

        total_time     += route_time
        total_distance += route_dist

        results.append({
            "driver":      driver["name"],
            "stops":       steps,
            "time_min":    int(route_time / 60),
            "distance_km": round(route_dist / 1000, 2)
        })

    return {
        "routes":             results,
        "total_time_min":     int(total_time / 60),
        "total_distance_km":  round(total_distance / 1000, 2),
        "unassigned":         unassigned
    }


# --------------------------
# SAFE WRAPPER
# --------------------------
def safe_optimize(func, drivers, passengers, destination):
    """
    Safely run an optimizer function.
    Never raises — always returns results or an error dict.
    """
    try:
        result = func(drivers, passengers, destination)
        if result is None:
            return {"error": ["Optimizer returned no result."]}
        return result
    except Exception as e:
        return {"error": [f"Optimizer error: {str(e)}"]}