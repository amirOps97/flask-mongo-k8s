# Flask + MongoDB Replica Set on Kubernetes

A production-style deployment of a Python Flask CRUD API backed by a MongoDB replica set, running on Kubernetes. This project covers the full lifecycle: containerization, orchestration, API documentation, and CI/CD with Jenkins.

Built as a hands-on DevOps learning project, every design decision is documented below — not just *what* was done, but *why*.


---

## Architecture

![Kubernetes Architecture](images/Image.png)

## Table of Contents

- [Concepts Covered](#concepts-covered)
- [Prerequisites](#prerequisites)
- [Project Structure](#project-structure)
- [Step-by-Step Setup Guide](#step-by-step-setup-guide)
  - [1. Create the Kubernetes Cluster](#1-create-the-kubernetes-cluster)
  - [2. Create Secrets](#2-create-secrets)
  - [3. Deploy MongoDB Replica Set](#3-deploy-mongodb-replica-set)
  - [4. Initialize the Replica Set](#4-initialize-the-replica-set)
  - [5. Deploy Mongo Express](#5-deploy-mongo-express)
  - [6. Build and Deploy the Flask App](#6-build-and-deploy-the-flask-app)
  - [7. Test the API](#7-test-the-api)
- [Deep Dive: Design Decisions](#deep-dive-design-decisions)
  - [Why StatefulSet for MongoDB?](#why-statefulset-for-mongodb)
  - [Headless Service and Pod DNS](#headless-service-and-pod-dns)
  - [KeyFile Authentication Between Replica Set Members](#keyfile-authentication-between-replica-set-members)
  - [The InitContainer Solution for KeyFile Permissions](#the-initcontainer-solution-for-keyfile-permissions)
  - [How the MongoDB Driver Routes Writes to the Primary](#how-the-mongodb-driver-routes-writes-to-the-primary)
  - [Overriding ENTRYPOINT and CMD with command and args](#overriding-entrypoint-and-cmd-with-command-and-args)
  - [Building the Connection String Without Hardcoding Secrets](#building-the-connection-string-without-hardcoding-secrets)
  - [Swagger and Flasgger: API Documentation](#swagger-and-flasgger-api-documentation)
- [CI/CD with Jenkins](#cicd-with-jenkins)
- [API Endpoints](#api-endpoints)

---

## Concepts Covered

- **Kubernetes**: StatefulSet, Deployment, Headless Service, NodePort, ConfigMap, Secret, InitContainer, Volumes (PV, PVC, Storage Class, EmptyDir), rolling updates, cross-namespace DNS, Namespaces
- **MongoDB**: Replica set, primary/secondary architecture, keyFile authentication
- **Docker**
- **Flask**: REST API, CRUD operations, pymongo driver, Flasgger
- **Swagger/OpenAPI**: API documentation with Flasgger, YAML docstrings
- **CI/CD**: Jenkins declarative pipeline (PaC)
---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/)
- A [Docker Hub](https://hub.docker.com/) account
- [Jenkins](https://www.jenkins.io/doc/book/installing/) (for CI/CD, optional)

---

## Project Structure

```
flask-mongo-k8s/
├── App.py                          # Flask application with CRUD endpoints
├── requirements.txt                # Python dependencies
├── Dockerfile                      # Container image definition
├── .dockerignore                   # Files excluded from Docker build
├── Jenkinsfile                     # CI/CD pipeline definition
├── k8s-flask-manifest/
│   ├── flask-cm.yaml           # Flask app configuration (MongoDB host, DB name)
│   ├── flask-deploy.yaml       # Flask app Deployment
│   └── flask-svc.yaml          # Flask app NodePort Service
└── k8s-mongodb-manifest/
    ├── mongodb-secret.yaml         # MongoDB credentials
    ├── mongodb-headless-svc.yaml   # Headless Service for StatefulSet DNS
    ├── mongodb-sts.yaml            # MongoDB 3-node replica set
```

---

## Step-by-Step Setup Guide

### 1. Create the Kubernetes Cluster

If using kind, create a config file to map the NodePort to your host:

```yaml
# kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
    extraPortMappings:
      - containerPort: 30080       # NodePort for Flask app
        hostPort: 30080            # maps to localhost:30080 on your machine
        protocol: TCP
```

```bash
kind create cluster --name flask-mongo --config kind-config.yaml
```

> **Why port mapping?** Kind runs Kubernetes inside Docker containers. Without `extraPortMappings`, the NodePort is only accessible inside the Docker network — not from your Mac's browser. This config maps port 30080 from the kind worker container to `localhost:30080` on your host machine.


### 2. Create Secrets

Create the MongoDB root credentials:

```bash
kubectl apply -f k8s-mongodb-manifest/mongodb-secret.yaml
```

Generate the keyFile for replica set internal authentication and store it as a secret:

```bash
openssl rand -base64 756 > mongo-keyfile
kubectl create secret generic mongo-keyfile-secret --from-file=mongo-keyfile
rm mongo-keyfile
```

> **Why a keyFile?** When MongoDB runs as a replica set with authentication enabled, the members need to prove to each other that they belong to the same cluster. The keyFile is a shared secret — every member has the same key, and they use it to authenticate inter-node communication. Without it, any rogue MongoDB instance could join your replica set.

### 3. Deploy MongoDB Replica Set

```bash
kubectl apply -f k8s-mongodb-manifest/mongodb-headless-svc.yaml
kubectl apply -f k8s-mongodb-manifest/mongodb-sts.yaml
```

Wait for all three pods to be running:

```bash
kubectl get pods -w
```

You should see:

```
NAME      READY   STATUS    RESTARTS   AGE
mongo-0   1/1     Running   0          60s
mongo-1   1/1     Running   0          45s
mongo-2   1/1     Running   0          30s
```

> **Note:** The pods start in order — `mongo-0` first, then `mongo-1`, then `mongo-2`. This is a StatefulSet behavior. Each pod waits for the previous one to be ready before starting. This ordered startup is important for databases.

### 4. Initialize the Replica Set

The three MongoDB pods are running, but they don't know about each other yet. You need to tell them they're a team:

```bash
kubectl exec -it mongo-0 -- mongosh -u admin -p password123 --eval '
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongo-0.mongo-svc:27017" },
    { _id: 1, host: "mongo-1.mongo-svc:27017" },
    { _id: 2, host: "mongo-2.mongo-svc:27017" }
  ]
})'
```

You should see `{ ok: 1 }`. Run this only on **one pod** — `mongo-0` distributes the configuration to the others and an election happens automatically.

Verify the replica set:

```bash
kubectl exec -it mongo-0 -- mongosh -u admin -p password123 --eval 'rs.status()'
```

You should see one member as `PRIMARY` and two as `SECONDARY`.


### 6. Build and Deploy the Flask App

Build and push the Docker image:

```bash
docker build -t DOCKERHUB_USERNAME/flask-mongo-app:1 .
docker push DOCKERHUB_USERNAME/flask-mongo-app:1
```

Update the image name in `k8s-flask-manifest/flask-deploy.yaml`, then deploy:

```bash
kubectl apply -f k8s-flask-manifest/
```

### 7. Test the API

Open Swagger UI at `http://<node-ip>:30080/apidocs` or use curl:

```bash
# Health check
curl http://<node-ip>:30080/health

# Create a product
curl -X POST http://<node-ip>:30050/api/products \
  -H "Content-Type: application/json" \
  -d '{"name": "Laptop", "description": "Gaming laptop 16GB RAM", "price": 999.99}'

# Get all products
curl http://<node-ip>:30080/api/products

# Get one product
curl http://<node-ip>:30080/api/products/<id>

# Update a product
curl -X PUT http://<node-ip>:30080/api/products/<id> \
  -H "Content-Type: application/json" \
  -d '{"price": 799.99}'

# Delete a product
curl -X DELETE http://<node-ip>:30080/api/products/<id>
```

---

## Deep Dive: Design Decisions

### Why StatefulSet for MongoDB?

A Deployment treats all pods as identical and interchangeable — if one dies, a new one gets a random name and a fresh volume. This is fine for stateless apps like our Flask API, but it breaks a database.

MongoDB needs three guarantees that only a StatefulSet provides:

1. **Stable network identity** — Each pod gets a predictable hostname (`mongo-0`, `mongo-1`, `mongo-2`) that doesn't change across restarts. The replica set members use these hostnames to find each other. If `mongo-1` restarted and came back as `mongo-xyz`, the other member

2. **Stable persistent storage** — Each pod gets its own PersistentVolumeClaim that follows it across restarts and rescheduling. If `mongo-0` is rescheduled to a different node, its data volume moves with it. In a Deployment, you'd lose the data.

3. **Ordered startup and shutdown** — Pods start in order (0, 1, 2) and terminate in reverse order (2, 1, 0). This matters for databases where the first node often has a special role (like initializing the replica set).

### Headless Service and Pod DNS

A normal Kubernetes Service gets a single ClusterIP and load-balances traffic across pods. That's useless for a database — you need to reach **specific** pods (the primary for writes, secondaries for reads).

A headless Service (`clusterIP: None`) doesn't get a ClusterIP. Instead, it creates individual DNS records for each pod:

```
mongo-0.mongo-svc.default.svc.cluster.local
mongo-1.mongo-svc.default.svc.cluster.local
mongo-2.mongo-svc.default.svc.cluster.local
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


### Overriding ENTRYPOINT and CMD with command and args

Understanding what `command` and `args` do:

- `command` overrides the Docker image's `ENTRYPOINT`
- `args` overrides the Docker image's `CMD`

By setting `command` to `/bin/sh -c`, we get a shell that can do `${VAR}` expansion. The shell reads the Secret values (already injected as environment variables), builds the full connection string, and then `exec node app` starts Mongo Express.


**Caution**: Overriding `command` bypasses the original Docker image's entrypoint script. If that script does important setup (creating directories, initializing configs), you'll break things. For Mongo Express it's fine because the entrypoint simply runs `node app`. For images with complex entrypoints (like Postgres or MySQL), you should call the original entrypoint explicitly: `exec docker-entrypoint.sh mongod --args`.

For the Flask app deployment, we used a cleaner approach — Kubernetes `$(VAR)` substitution:

```yaml
- name: MONGO_URI
  value: "mongodb://$(MONGO_USER):$(MONGO_PASS)@$(MONGO_HOST)/$(MONGO_DATABASE)?replicaSet=$(MONGO_REPLICA_SET)&authSource=$(MONGO_AUTH_SOURCE)"
```

This is not shell interpolation — it's resolved by the Kubernetes API server before the container starts. `$(VAR_NAME)` references env vars defined earlier in the same spec. No shell override needed, and the original image entrypoint runs untouched.


### How the MongoDB Driver Routes Writes to the Primary

A common question: if you list all three MongoDB nodes in the connection string, how does the driver know which one is the primary?

```
mongodb://admin:pass@mongo-0:27017,mongo-1:27017,mongo-2:27017/?replicaSet=rs0
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

- **`kubectl set image` over `kubectl apply`**: For CI/CD, only the image tag changes between deployments. `kubectl set image` updates just the image without touching the rest of the manifest. `kubectl apply` would require modifying the YAML file first.

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

- [X] **Liveness and readiness probes** — Let Kubernetes detect unhealthy pods automatically
- [ ] **Helm charts** — Package all manifests into a reusable, configurable chart
- [ ] **Ingress controller** — Replace NodePort with proper HTTP routing
- [x] **Namespaces** — Separate database and application workloads
- [ ] **Resource limits** — CPU and memory requests/limits on all pods
- [ ] **External Secrets Operator** — Fetch secrets from HashiCorp Vault or AWS Secrets Manager instead of storing them in manifests
- [ ] **Prometheus + Grafana** — Monitoring and alerting
- [ ] **ArgoCD** — GitOps-based deployment instead of direct kubectl from CI

---

## License


