# Flask + MongoDB Replica Set on Kubernetes

A production-style deployment of a Python Flask CRUD API backed by a MongoDB replica set, running on Kubernetes. This project covers the full lifecycle: containerization, orchestration, API documentation, and CI/CD with Jenkins.

Built as a hands-on DevOps learning project, every design decision is documented below — not just *what* was done, but *why*.

---

## Architecture

![Kubernetes Architecture](images/Image.png)
---

## Table of Contents
- [Prerequisites](#prerequisites)
- [Step-by-Step Setup Guide](#step-by-step-setup-guide)
  - [1. Create the Kubernetes Cluster](#1-create-the-kubernetes-cluster)
  - [2. Install Nginx Ingress Controller](#2-install-nginx-ingress-controller)
  - [3. Install Metrics Server](#3-install-metrics-server)
  - [4. Create Namespaces](#4-create-namespaces)
  - [5. Create Secrets](#5-create-secrets)
  - [6. Deploy MongoDB Replica Set](#6-deploy-mongodb-replica-set)
  - [7. Initialize the Replica Set](#7-initialize-the-replica-set)
  - [8. Build and Deploy the Flask App](#8-build-and-deploy-the-flask-app)
  - [9. Create the Ingress Resource](#9-create-the-ingress-resource)
  - [10. Test the API](#10-test-the-api)
- [Deep Dive: Design Decisions](#deep-dive-design-decisions)
  - [Why StatefulSet for MongoDB?](#why-statefulset-for-mongodb)
  - [Headless Service and Pod DNS](#headless-service-and-pod-dns)
  - [KeyFile Authentication Between Replica Set Members](#keyfile-authentication-between-replica-set-members)
  - [The InitContainer Solution for KeyFile Permissions](#the-initcontainer-solution-for-keyfile-permissions)
  - [How the MongoDB Driver Routes Writes to the Primary](#how-the-mongodb-driver-routes-writes-to-the-primary)
  - [Overriding ENTRYPOINT and CMD with command and args](#overriding-entrypoint-and-cmd-with-command-and-args)
  - [Building the Connection String Without Hardcoding Secrets](#building-the-connection-string-without-hardcoding-secrets)
  - [Swagger and Flasgger: API Documentation](#swagger-and-flasgger-api-documentation)
  - [Namespace Separation and Cross-Namespace DNS](#namespace-separation-and-cross-namespace-dns)
  - [Ingress: How External Traffic Enters the Cluster](#ingress-how-external-traffic-enters-the-cluster)
  - [Resource Management: Requests, Limits, and Autoscaling](#resource-management-requests-limits-and-autoscaling)
- [CI/CD with Jenkins](#cicd-with-jenkins)
- [API Endpoints](#api-endpoints)

---

## Concepts Covered

- **Kubernetes**: StatefulSet, Deployment, Headless Service, ClusterIP, ConfigMap, Secret, InitContainer, Volumes (PV, PVC, Storage Class, EmptyDir), Namespaces, HPA, resource requests/limits, cross-namespace DNS, Ingress, rolling updates
- **MongoDB**: Replica set, primary/secondary architecture, keyFile authentication
- **Docker**
- **Flask**: REST API, CRUD operations, pymongo driver, Flasgger
- **Swagger/OpenAPI**: API documentation with Flasgger, YAML docstrings
- **CI/CD**: Jenkins declarative pipeline (PaC)
- **Networking**: DNS resolution in Kubernetes, hostNetwork, Ingress controller routing

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/)
- A [Docker Hub](https://hub.docker.com/) account
- [Jenkins](https://www.jenkins.io/doc/book/installing/) (for CI/CD, optional)

---


## Step-by-Step Setup Guide

### 1. Create the Kubernetes Cluster

Create a Kind config that maps port 80 and 443 on the worker node to your host machine. This is needed because the Ingress controller will listen on these ports via `hostNetwork`:

```yaml
# kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
    extraPortMappings:
      - containerPort: 80        # HTTP traffic for Ingress
        hostPort: 80
        protocol: TCP
      - containerPort: 443       # HTTPS traffic for Ingress
        hostPort: 443
        protocol: TCP
```

```bash
kind create cluster --name flask-mongo --config kind-config.yaml
```

> **Why port mapping on the worker?** Kind runs Kubernetes nodes as Docker containers. The Ingress controller runs on the worker node with `hostNetwork: true`, meaning it binds directly to the node's port 80. The `extraPortMappings` bridges that port from the Docker container to `localhost:80` on your machine. Without it, port 80 inside the Kind container is unreachable from your browser.

### 2. Install Nginx Ingress Controller

The standard Kind ingress manifest needs two modifications for our setup. Download it, apply the patches, then install:

```bash
# Download the manifest
curl -sL https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/kind/deploy.yaml -o ingress-nginx.yaml
```

Make an edit in `ingress-nginx.yaml` — find the `ingress-nginx-controller` Deployment's pod spec:

**Edit** — Remove `ingress-ready: "true"` from `nodeSelector` and add `hostNetwork: true`:

```yaml
      hostNetwork: true          # <-- add this
      nodeSelector:
        kubernetes.io/os: linux  # <-- remove the ingress-ready line, keep only this
```

Apply and wait for it to be ready:

```bash
kubectl apply -f ingress-nginx.yaml

kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s
```

Verify the controller is running on the worker node:

```bash
kubectl get pods -n ingress-nginx -o wide
```

You should see the controller pod on `flask-mongo-worker` with an IP like `172.18.x.x` (the node's IP, not a pod network IP — confirming `hostNetwork` is active).

> **Why `hostNetwork: true`?** Normally, pods get their own IP in the pod network (10.244.x.x) and need a Service to be reachable. With `hostNetwork: true`, the pod shares the node's network namespace — port 80 on the pod IS port 80 on the node. This eliminates the need for a LoadBalancer service (which requires a cloud provider or MetalLB) and works perfectly with Kind's port mapping.

### 3. Install Metrics Server

Kind doesn't include a metrics server by default. It's required for `kubectl top` and for the HorizontalPodAutoscaler to read CPU/memory usage from pods:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

For Kind, the metrics server needs a flag to skip TLS verification (since Kind uses self-signed certificates). Patch the deployment:

```bash
kubectl patch deployment metrics-server -n kube-system \
  --type='json' \
  -p='[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]'
```

Wait for it to be ready:

```bash
kubectl wait --namespace kube-system \
  --for=condition=ready pod \
  --selector=k8s-app=metrics-server \
  --timeout=90s
```

Verify it's working:

```bash
kubectl top nodes
kubectl top pods -A
```

> **Why `--kubelet-insecure-tls`?** The metrics server scrapes metrics from the kubelet on each node via HTTPS. In Kind, the kubelet uses self-signed certificates that the metrics server doesn't trust by default. This flag tells it to skip TLS verification. In production clusters (EKS, GKE, AKS), proper certificates are configured and this flag is not needed.


### 4. Create Namespaces

```bash
kubectl create namespace database
kubectl create namespace app
```

> **Why namespaces?** In production, you wouldn't run your database and application in the same namespace. Namespaces provide resource isolation, access control boundaries, and organizational clarity. They also let different teams manage their own resources independently.

### 5. Create Secrets

Create the MongoDB root credentials in both namespaces. The `database` namespace needs them for MongoDB itself, and the `app` namespace needs them for the Flask app's connection string. Kubernetes Secrets can't be shared across namespaces, so both need a copy:

```bash
kubectl apply -f k8s-mongodb-manifest/mongodb-secret.yaml
kubectl apply -f k8s-flask-manifest/mongodb-secret.yaml
```

Generate the keyFile for replica set internal authentication:

```bash
openssl rand -base64 756 > mongo-keyfile
kubectl create secret generic mongo-keyfile-secret --from-file=mongo-keyfile -n database
rm mongo-keyfile
```

> **Why a keyFile?** When MongoDB runs as a replica set with authentication enabled, the members need to prove to each other that they belong to the same cluster. The keyFile is a shared secret — every member has the same key, and they use it to authenticate inter-node communication. Without it, any rogue MongoDB instance could join your replica set.

### 6. Deploy MongoDB Replica Set

```bash
kubectl apply -f k8s-mongodb-manifest/
```

Wait for all three pods to be running:

```bash
kubectl get pods -n database -w
```

You should see:

```
NAME      READY   STATUS    RESTARTS   AGE
mongo-0   1/1     Running   0          60s
mongo-1   1/1     Running   0          45s
mongo-2   1/1     Running   0          30s
```

> **Note:** The pods start in order — `mongo-0` first, then `mongo-1`, then `mongo-2`. This is a StatefulSet behavior. Each pod waits for the previous one to be ready before starting. This ordered startup is important for databases.

### 7. Initialize the Replica Set

The three MongoDB pods are running, but they don't know about each other yet. You need to tell them they're a team:

```bash
kubectl exec -n database -it mongo-0 -- mongosh -u admin -p password123 --eval '
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongo-0.mongo-svc.database:27017" },
    { _id: 1, host: "mongo-1.mongo-svc.database:27017" },
    { _id: 2, host: "mongo-2.mongo-svc.database:27017" }
  ]
})'
```

You should see `{ ok: 1 }`. Run this only on **one pod** — `mongo-0` distributes the configuration to the others and an election happens automatically.

Verify the replica set:

```bash
kubectl exec -n database -it mongo-0 -- mongosh -u admin -p password123 --eval 'rs.status()'
```

You should see one member as `PRIMARY` and two as `SECONDARY`.

### 8. Build and Deploy the Flask App

Build and push the Docker image:

```bash
docker build -t YOUR_DOCKERHUB_USERNAME/flask-mongo-app:1 .
docker push YOUR_DOCKERHUB_USERNAME/flask-mongo-app:1
```

Update the image name in `k8s-flask-manifest/flask-deploy.yaml`, then deploy:

```bash
kubectl apply -f k8s-flask-manifest/
```

### 9. Create the Ingress Resource

The Ingress resource tells the nginx controller how to route traffic:

```yaml
# k8s-flask-manifest/flask-ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: flask-ingress
  namespace: app
spec:
  ingressClassName: nginx
  rules:
    - host: flask-app.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: flask-app-svc
                port:
                  number: 5000
```

If you already ran `kubectl apply -f k8s-flask-manifest/` in the previous step, the Ingress resource is already applied. Otherwise:

```bash
kubectl apply -f k8s-flask-manifest/flask-ingress.yaml
```

Add the hostname to your machine's hosts file:

```bash
sudo sh -c 'echo "127.0.0.1 flask-app.local" >> /etc/hosts'
```

Verify the Ingress is configured:

```bash
kubectl get ingress -n app
```

### 10. Test the API

Open Swagger UI in your browser at `http://flask-app.local/apidocs` or use curl:

```bash
# Health check
curl http://flask-app.local/health

# Create a product
curl -X POST http://flask-app.local/api/products \
  -H "Content-Type: application/json" \
  -d '{"name": "Laptop", "description": "Gaming laptop 16GB RAM", "price": 999.99}'

# Get all products
curl http://flask-app.local/api/products

# Get one product
curl http://flask-app.local/api/products/<id>

# Update a product
curl -X PUT http://flask-app.local/api/products/<id> \
  -H "Content-Type: application/json" \
  -d '{"price": 799.99}'

# Delete a product
curl -X DELETE http://flask-app.local/api/products/<id>
```

---

## Deep Dive: Design Decisions

### Why StatefulSet for MongoDB?

A Deployment treats all pods as identical and interchangeable — if one dies, a new one gets a random name and a fresh volume. This is fine for stateless apps like our Flask API, but it breaks a database.

MongoDB needs three guarantees that only a StatefulSet provides:

1. **Stable network identity** — Each pod gets a predictable hostname (`mongo-0`, `mongo-1`, `mongo-2`) that doesn't change across restarts. The replica set members use these hostnames to find each other. If `mongo-1` restarted and came back as `mongo-xyz`, the other members wouldn't recognize it.

2. **Stable persistent storage** — Each pod gets its own PersistentVolumeClaim that follows it across restarts and rescheduling. If `mongo-0` is rescheduled to a different node, its data volume moves with it. In a Deployment, you'd lose the data.

3. **Ordered startup and shutdown** — Pods start in order (0, 1, 2) and terminate in reverse order (2, 1, 0). This matters for databases where the first node often has a special role (like initializing the replica set).

### Headless Service and Pod DNS

A normal Kubernetes Service gets a single ClusterIP and load-balances traffic across pods. That's useless for a database — you need to reach **specific** pods (the primary for writes, secondaries for reads).

A headless Service (`clusterIP: None`) doesn't get a ClusterIP. Instead, it creates individual DNS records for each pod:

```
mongo-0.mongo-svc.database.svc.cluster.local
mongo-1.mongo-svc.database.svc.cluster.local
mongo-2.mongo-svc.database.svc.cluster.local
```

This is how the MongoDB driver can address each replica set member individually. The headless Service is the glue between Kubernetes networking and MongoDB's replica set protocol.

### KeyFile Authentication Between Replica Set Members

When you enable authentication in MongoDB (`MONGO_INITDB_ROOT_USERNAME/PASSWORD`), clients need credentials to connect. But there's a second layer: the replica set members themselves need to authenticate with each other during replication and elections.

MongoDB uses a **keyFile** for this — a shared secret file that every member holds. When `mongo-1` tries to replicate data from `mongo-0`, it presents the key. If the key matches, replication proceeds. If not, the connection is rejected.

The keyFile is generated with `openssl rand -base64 756` and stored as a Kubernetes Secret. Each MongoDB pod mounts it and passes `--keyFile /path/to/keyfile` to the `mongod` process.

**MongoDB is extremely strict about keyFile permissions**: the file must be owned by the MongoDB user (UID 999) with mode `0400` (owner read only). Any other permission and MongoDB refuses to start with "permissions on keyfile are too open." This strictness led us to the InitContainer solution described next.

### The InitContainer Solution for KeyFile Permissions

This was the trickiest part of the deployment. Here's the problem:

When Kubernetes mounts a Secret as a volume, the files are always owned by `root:root`. You can set the `defaultMode` (file permissions), but you **cannot** set the owner. MongoDB (UID 999) needs to own the file with mode `0400`.

We tried several approaches using Security Context:

| Approach | Result |
|----------|--------|
| `defaultMode: 0400` | File owned by root — MongoDB can't read it |
| `defaultMode: 0400` + `fsGroup: 999` | Group changed to 999, but mode is owner-read only — still can't read |
| `defaultMode: 0440` + `fsGroup: 999` | MongoDB can read it, but rejects "permissions too open" |
| `runAsUser: 999` | Changes who runs the process, not who owns the file — still root-owned |

The solution: an **InitContainer** that runs before the main MongoDB container:

```yaml
initContainers:
  - name: fix-keyfile-permissions
    image: busybox
    command: ["/bin/sh", "-c"]
    args:
      - |
        cp /keyfile-secret/mongo-keyfile /keyfile/mongo-keyfile
        chmod 400 /keyfile/mongo-keyfile
        chown 999:999 /keyfile/mongo-keyfile
```

The InitContainer copies the keyFile from the Secret volume to an `emptyDir` volume, sets `chmod 400` and `chown 999:999`, then exits. The main MongoDB container mounts the same `emptyDir` and finds the file with perfect permissions.

The `emptyDir` survives after the InitContainer terminates because it's tied to the **pod's** lifecycle, not any individual container's. It's only deleted when the pod itself is deleted.

### How the MongoDB Driver Routes Writes to the Primary

A common question: if you list all three MongoDB nodes in the connection string, how does the driver know which one is the primary?

```
mongodb://admin:pass@mongo-0.mongo-svc.database:27017,mongo-1.mongo-svc.database:27017,mongo-2.mongo-svc.database:27017/?replicaSet=rs0
```

These are **seed nodes**, not destinations. The driver doesn't blindly send queries to whichever IP DNS resolves. Here's what actually happens:

1. The driver connects to any one of the listed nodes
2. It asks: "What's the replica set topology?"
3. That node responds: "I'm a secondary. `mongo-0` is the primary. `mongo-2` is another secondary."
4. The driver caches this internal map
5. **All writes go directly to the primary** — the driver knows exactly which node it is
6. Reads go to the primary or secondaries based on read preference configuration
7. If the primary fails and a new election happens, the driver detects this automatically and updates its internal map

So the intelligence lives in the **MongoDB driver** (pymongo in our case), not in Kubernetes DNS or the Service. Kubernetes just provides the network plumbing — the driver handles routing.

### Overriding ENTRYPOINT and CMD with command and args

Kubernetes lets you override a Docker image's startup behavior:

- `command` overrides the Docker image's `ENTRYPOINT`
- `args` overrides the Docker image's `CMD`

We use this in the MongoDB StatefulSet to pass replica set configuration flags:

```yaml
args: ["--replSet", "rs0", "--bind_ip_all", "--keyFile", "/keyfile/mongo-keyfile"]
```

The `mongo:7` image's Dockerfile has `CMD ["mongod"]`. By setting `args`, we override it so the container runs `mongod --replSet rs0 --bind_ip_all --keyFile /keyfile/mongo-keyfile`. The entrypoint script detects these are `mongod` flags and prepends `mongod` automatically.

Another common pattern is overriding `command` to get a shell for runtime string interpolation:

```yaml
command: ["/bin/sh", "-c"]
args:
  - |
    export CONNECTION_URL="mongodb://${DB_USER}:${DB_PASS}@host:27017"
    exec node app
```

By setting `command` to `/bin/sh -c`, you get a shell that can do `${VAR}` expansion from environment variables. The `exec` keyword is important — it **replaces** the shell process with the application process. Without it, you'd have an unnecessary parent shell hanging around.

**Caution**: Overriding `command` bypasses the original Docker image's entrypoint script. If that script does important setup (creating directories, initializing configs), you'll break things. For images with complex entrypoints (like Postgres or MySQL), call the original entrypoint explicitly: `exec docker-entrypoint.sh <command> --args`.

For the Flask app deployment, we used a cleaner approach — Kubernetes `$(VAR)` substitution:

```yaml
- name: MONGO_URI
  value: "mongodb://$(MONGO_USER):$(MONGO_PASS)@$(MONGO_HOST)/$(MONGO_DATABASE)?replicaSet=$(MONGO_REPLICA_SET)&authSource=$(MONGO_AUTH_SOURCE)"
```

This is not shell interpolation — it's resolved by the Kubernetes API server before the container starts. `$(VAR_NAME)` references env vars defined earlier in the same spec. No shell override needed, and the original image entrypoint runs untouched.

### Building the Connection String Without Hardcoding Secrets

Credentials should never appear in your YAML manifests. This project uses two patterns:

**Pattern 1 — Kubernetes `$(VAR)` substitution (Flask app):**

Environment variables from Secrets and ConfigMaps are defined first, then composed into the connection string using `$(VAR_NAME)`:

```yaml
env:
  - name: MONGO_USER
    valueFrom:
      secretKeyRef: { name: mongo-secret, key: mongo-root-username }
  - name: MONGO_PASS
    valueFrom:
      secretKeyRef: { name: mongo-secret, key: mongo-root-password }
  - name: MONGO_HOST
    valueFrom:
      configMapKeyRef: { name: flask-app-config, key: MONGO_HOST }
  - name: MONGO_URI
    value: "mongodb://$(MONGO_USER):$(MONGO_PASS)@$(MONGO_HOST)/..."
```

Order matters — `MONGO_URI` must be defined **after** the variables it references.

**Pattern 2 — Shell expansion:**

When Kubernetes substitution isn't flexible enough, a shell startup script builds the string from env vars at runtime.

Both patterns keep credentials in Secrets, which are base64-encoded in the cluster and never committed to Git.

**Note on `authSource`**: MongoDB creates the root user in the `admin` database. If your connection string points to a different database (like `productdb`), you must add `authSource=admin` — otherwise MongoDB tries to authenticate against `productdb` where the user doesn't exist, and you get "Authentication failed."

### Swagger and Flasgger: API Documentation

**OpenAPI** (formerly called Swagger) is a standard format for describing REST APIs — a structured YAML/JSON document that lists every endpoint, its parameters, request body, and responses.

**Swagger UI** is a tool that reads an OpenAPI spec and renders an interactive web page where you can explore and test the API.

**Flasgger** is a Flask extension that combines both: it generates the OpenAPI spec from your code and serves Swagger UI at `/apidocs`.

**Important**: Flasgger does **not** automatically scan your routes and generate full documentation. It needs explicit YAML docstrings inside each route function:

```python
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
          properties:
            name:
              type: string
              example: "Laptop"
    responses:
      201:
        description: Product created
    """
```

The `---` separator is critical — it tells Flasgger "YAML starts here." Without these docstrings, Swagger UI shows an empty page with no endpoints. The docstrings describe what the endpoint accepts and returns, and Flasgger turns this into the interactive documentation.

### Namespace Separation and Cross-Namespace DNS

This project separates resources into two namespaces: `database` for MongoDB and `app` for the Flask API. This mirrors production environments where database and application workloads are managed independently with different access controls.

When pods communicate within the same namespace, short service names work:

```
mongo-0.mongo-svc          ← works inside the database namespace
```

When the Flask app (in the `app` namespace) needs to reach MongoDB (in the `database` namespace), it must include the namespace in the DNS name:

```
mongo-0.mongo-svc.database
```

The full FQDN is `mongo-0.mongo-svc.database.svc.cluster.local`, but the short form with just the namespace appended works fine.

This means the ConfigMap must specify cross-namespace hostnames:

```yaml
MONGO_HOST: "mongo-0.mongo-svc.database:27017,mongo-1.mongo-svc.database:27017,mongo-2.mongo-svc.database:27017"
```

One caveat: Kubernetes Secrets don't cross namespace boundaries. The Flask app needs MongoDB credentials, but the Secret lives in the `database` namespace. The solution is to maintain a copy of the Secret in the `app` namespace. In production, the External Secrets Operator eliminates this duplication by letting both namespaces pull from the same external source (like HashiCorp Vault).

### Ingress: How External Traffic Enters the Cluster

Before Ingress, we used NodePort to expose the Flask app — traffic hit `localhost:30080` and was routed to the pod. This works but has limitations: ugly port numbers (30000-32767), one port per service, no hostname-based routing, and no SSL termination.

Ingress solves all of these. It has two components:

**Ingress Controller** — an nginx pod that acts as a reverse proxy. It's installed once per cluster and watches for Ingress resources across all namespaces. In our Kind setup, it runs on the worker node with `hostNetwork: true`, meaning it binds directly to port 80 on the node — no LoadBalancer or NodePort service needed.

**Ingress Resource** — a YAML config that defines routing rules. It tells the controller "when someone requests `flask-app.local`, route traffic to the `flask-app-svc` service in the `app` namespace."

The full traffic flow:

```
Browser (http://flask-app.local)
  │
  │  /etc/hosts resolves flask-app.local → 127.0.0.1
  │
  ▼
Kind port mapping (localhost:80 → worker container:80)
  │
  ▼
Nginx pod (hostNetwork, matches host: flask-app.local)
  │
  │  Reads Ingress resource rules
  │
  ▼
ClusterIP Service (flask-app-svc:5000)
  │
  ▼
Flask pod (:5000)
```

With this setup, the Flask service is a ClusterIP (internal only) instead of NodePort — no ports are directly exposed on nodes. All external traffic flows through the Ingress controller, which is the production pattern.

**Why `hostNetwork` instead of LoadBalancer?** In cloud environments (AWS, GKE, AKS), the Ingress controller uses a LoadBalancer service that provisions a real external load balancer. On bare metal, MetalLB fills that role. Kind has neither, so `hostNetwork: true` is the shortcut — the pod binds directly to the node's port 80, and Kind's Docker port mapping bridges it to your Mac. The Ingress Resource YAML stays identical regardless of how the controller is exposed.

**Why the manifest needed patching:** The upstream Kind manifest had `ingress-ready: "true"` as a nodeSelector (targeting the control plane). We removed the `ingress-ready` selector to let the pod schedule on the worker node (where our port mapping is), added `hostNetwork: true`, and updated the toleration to `control-plane` for Kubernetes v1.24+.

### Resource Management: Requests, Limits, and Autoscaling

Every container in this project has resource requests and limits configured:

```yaml
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 256Mi
```

**Requests** are the minimum guaranteed resources. The Kubernetes scheduler uses requests to decide which node to place a pod on — it won't schedule a pod on a node that doesn't have enough available capacity. If you request `100m` CPU (100 millicores, or 10% of a core), the scheduler guarantees that capacity.

**Limits** are the maximum a container can use. If a container tries to exceed its CPU limit, it gets throttled (slowed down). If it exceeds its memory limit, it gets OOMKilled (terminated and restarted). Limits prevent a misbehaving container from starving other pods on the same node.

**Why both?** Without requests, the scheduler has no idea how much a pod needs and might pack too many onto one node. Without limits, a single pod could consume all of a node's resources and crash everything else. Together, they provide both scheduling intelligence and runtime safety.

**HorizontalPodAutoscaler (HPA)** automatically scales the Flask deployment based on observed CPU usage:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: flask-app-hpa
  namespace: app
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: flask-app
  minReplicas: 1
  maxReplicas: 5
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 50
```

This tells Kubernetes: "if average CPU across all Flask pods exceeds 50% of the **requested** CPU, add more pods. Scale between 1 and 5 replicas." With a CPU request of `100m`, the HPA triggers scaling when average usage exceeds `50m` per pod.

The HPA depends on the **metrics server** to read real-time CPU and memory usage from pods. Without the metrics server installed, the HPA shows `<unknown>/50%` in its targets and never scales.

**Why HPA only on Flask, not MongoDB?** HPA scales horizontally — it adds more pods. This makes sense for stateless workloads like the Flask app where any pod can handle any request. A MongoDB replica set has a fixed number of members (3) defined by `rs.initiate()`. Adding a random pod wouldn't automatically join it to the replica set. Database scaling requires a different approach — either vertical scaling (more CPU/memory per pod) or sharding.

To test autoscaling, generate load and watch the HPA respond:

```bash
# In one terminal — generate traffic
kubectl run load-test --image=busybox -n app --rm -it -- \
  /bin/sh -c "while true; do wget -q -O- http://flask-app-svc:5000/api/products; done"

# In another terminal — watch the HPA
kubectl get hpa -n app -w
```

You should see CPU percentage climb, replica count increase, and after stopping the load test, scale back down after the cooldown period (default 5 minutes).


---

## CI/CD with Jenkins

The project includes a `Jenkinsfile` that automates the build-push-deploy cycle:

```
Code Push → Jenkins detects change → Build Docker image → Push to Docker Hub → Update Kubernetes deployment
```

### Pipeline Stages

1. **Build Docker Image** — Runs `docker build` and tags the image with the Jenkins build number
2. **Push to Docker Hub** — Authenticates with Docker Hub using Jenkins credentials and pushes the image
3. **Deploy to Kubernetes** — Runs `kubectl set image` to update the running deployment with the new image tag, then waits for the rollout to complete

### Key Design Decisions in the Pipeline

- **Shell commands over plugins**: The pipeline uses `sh` steps (`docker build`, `docker push`, `kubectl`) instead of Jenkins-specific plugin DSL. This makes the pipeline portable — the same commands work in GitHub Actions, GitLab CI, or any CI system.

- **Build number as image tag**: Each build produces a uniquely tagged image (`flask-mongo-app:1`, `flask-mongo-app:2`, etc.) instead of using `latest`. This makes deployments traceable and rollbacks trivial: `kubectl set image deployment/flask-app flask-app=image:5` to roll back to build 5.

- **`kubectl set image` over `kubectl apply`**: For CI/CD, only the image tag changes between deployments. `kubectl set image` updates just the image without touching the rest of the manifest. `kubectl apply` would require modifying the YAML file first. Note the `-n app` flag since the deployment lives in the `app` namespace.

- **Rollout status check**: After updating the image, the pipeline runs `kubectl rollout status --timeout=60s` to wait for the new pods to be ready. If the deployment fails (crash loop, image pull error), the pipeline fails too — giving you immediate feedback.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/api/products` | Create a product |
| GET | `/api/products` | List all products |
| GET | `/api/products/<id>` | Get a product by ID |
| PUT | `/api/products/<id>` | Update a product |
| DELETE | `/api/products/<id>` | Delete a product |

### Request Body (POST/PUT)

```json
{
  "name": "Laptop",
  "description": "Gaming laptop 16GB RAM",
  "price": 999.99
}
```

---



## What's Next

Planned improvements to make this more production-ready:

- [x] **Liveness and readiness probes** — Let Kubernetes detect unhealthy pods automatically
- [ ] **Helm charts** — Package all manifests into a reusable, configurable chart
- [x] **Ingress controller** — Replace NodePort with proper HTTP routing
- [x] **Namespaces** — Separate database and application workloads
- [x] **Resource limits and HPA** — CPU/memory requests and limits on all pods, horizontal autoscaling for Flask
- [ ] **External Secrets Operator** — Fetch secrets from HashiCorp Vault or AWS Secrets Manager instead of storing them in manifests
- [ ] **Prometheus + Grafana** — Monitoring and alerting
- [ ] **ArgoCD** — GitOps-based deployment instead of direct kubectl from CI
---

