# Deployment runbook

Written for: whoever deploys this, including the author six months from now.

Target: **Cloud Run, `asia-south1` (Mumbai)** — lowest latency for Indian users
and for the India-region data.

## Before anything: two steps that silently break everything

These are the two failure modes that are invisible in the code, so they are
first rather than buried in the middle.

**1. The Cloud project must be registered with Earth Engine, and the runtime
service account must hold IAM roles on it.** Enabling the API is not enough.
Register the project at <https://code.earthengine.google.com/register>; the
registration covers principals that have access to it, so the service account
then needs `roles/earthengine.writer` and
`roles/serviceusage.serviceUsageConsumer`. Miss either and every request
returns 403. `/api/ready` reports which of the two is missing.

**2. The Workload Identity Federation provider must carry an attribute
condition pinning the repository.** Without it, *any* GitHub repository can
impersonate your deploy service account. This is the single most common WIF
misconfiguration and it is a full compromise of the project, not a degradation.

```
--attribute-condition="assertion.repository == 'OWNER/REPO'"
```

## One-time GCP setup

Roughly fifteen commands. Deliberately not Terraform: one service, and the
runbook is what actually gets read.

```bash
export PROJECT_ID=pv-mapping-india
export REGION=asia-south1
export REPO=OWNER/REPO            # your GitHub owner/repo
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')

gcloud config set project $PROJECT_ID

# --- APIs ---
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  earthengine.googleapis.com \
  firestore.googleapis.com \
  iamcredentials.googleapis.com

# --- image registry ---
gcloud artifacts repositories create solaris \
  --repository-format=docker --location=$REGION

# --- two service accounts, with different jobs ---
# Runtime: what the container runs as. Needs Earth Engine and Firestore.
gcloud iam service-accounts create solaris-runtime \
  --display-name="SOLARIS Cloud Run runtime"

# CI: what GitHub Actions impersonates. Needs to deploy, not to read data.
gcloud iam service-accounts create solaris-ci \
  --display-name="SOLARIS CI deployer"

export RUNTIME_SA=solaris-runtime@$PROJECT_ID.iam.gserviceaccount.com
export CI_SA=solaris-ci@$PROJECT_ID.iam.gserviceaccount.com

for ROLE in roles/earthengine.writer roles/datastore.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$RUNTIME_SA" --role="$ROLE"
done

for ROLE in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$CI_SA" --role="$ROLE"
done

# --- keyless CI auth: no JSON key anywhere ---
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '$REPO'"   # <-- step 2 above

gcloud iam service-accounts add-iam-policy-binding $CI_SA \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

# --- Firestore, for the persistent cache and the shared budget counter ---
gcloud firestore databases create --location=$REGION

# The TTL policy. Without it the cache still works -- expiry is also checked in
# Python -- but nothing ever gets deleted, so this costs storage, not
# correctness.
gcloud firestore fields ttls update expires_at \
  --collection-group=solaris-cache --enable-ttl
```

Then set these as GitHub Actions **variables** (not secrets — none is sensitive):

| Variable | Value | Required |
|---|---|:-:|
| `WIF_PROVIDER` | `projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github` | yes |
| `DEPLOY_SERVICE_ACCOUNT` | `solaris-ci@$PROJECT_ID.iam.gserviceaccount.com` | yes |
| `RUNTIME_SERVICE_ACCOUNT` | `solaris-runtime@$PROJECT_ID.iam.gserviceaccount.com` | yes |
| `GCP_PROJECT` | your project id, for the image path | yes |
| `GEE_PROJECT_ID` | your project id, for `ee.Initialize` | yes |
| `PUBLIC_URL` | a custom domain, e.g. `https://solaris.example` | no |

`GCP_PROJECT` and `GEE_PROJECT_ID` are ordinarily the same value. They are
separate variables because the image registry path and the Earth Engine project
are independent choices, and a deployment could legitimately push images to one
project while billing Earth Engine to another.

`PUBLIC_URL` is optional: Cloud Run assigns the service URL, so it cannot be a
prerequisite of the first deploy. The workflow reads the URL back and sets
`SOLARIS_CORS_ORIGINS` from it, and `PUBLIC_URL` overrides that for a custom
domain.

The deploy job verifies every required variable is set before it authenticates,
so a missing one fails in seconds with the name rather than surfacing as an
opaque push error.

**No service-account JSON key is created at any point.** If you find yourself
downloading one, something has gone wrong with the WIF setup — fix that instead.

## Deploying

Deployment is **manual**: run the *Deploy to Cloud Run* workflow from the
Actions tab and type `deploy` to confirm. It builds the image, deploys,
smoke-tests `/api/health` and `/api/ready`, and rolls back to the previous
revision on failure.

It does not run on push. An earlier version did, with no dependency on CI, so a
commit with failing tests would have deployed.

Manually:

```bash
gcloud run deploy solaris \
  --source . \
  --region=$REGION \
  --service-account=$RUNTIME_SA \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=8 \
  --cpu-boost \
  --timeout=120 \
  --set-env-vars="SOLARIS_ENV=prod,SOLARIS_CACHE_BACKEND=firestore,SOLARIS_LOG_FORMAT=json,GEE_PROJECT_ID=$PROJECT_ID,SOLARIS_CORS_ORIGINS=https://your-domain"
```

### Why those flags

| Flag | Reason |
|---|---|
| `--min-instances=0` | Near-zero idle cost. Pinning to 1 to hide cold starts costs ~$10–15/month and defeats the point. |
| `--cpu-boost` | Absorbs the `ee.Initialize()` cold start instead, for free. |
| `--max-instances=2` | A hard damage ceiling. It also makes the in-process rate limiter near-global: with ≤2 instances the effective limit is ≤2×, which is ample precision. |
| `--concurrency=8` | Every endpoint is a sync `def`, so FastAPI runs it on a 40-thread pool. Combined with the in-process semaphore this bounds concurrent Earth Engine calls. |
| `--timeout=120` | Above the request timeout, so Cloud Run is not the thing that kills a slow request. |

## Configuration

Full list in `.env.example`. The ones that matter in production:

| Variable | Production value | Note |
|---|---|---|
| `SOLARIS_ENV` | `prod` | |
| `GEE_PROJECT_ID` | your project | **Server-side only.** Deliberately not accepted from requests — it previously was, which let any caller choose which project to initialise and whose quota to spend. |
| `SOLARIS_CACHE_BACKEND` | `firestore` | Tiered: memory in front of Firestore. |
| `SOLARIS_CORS_ORIGINS` | your domain | A wildcard is **rejected at startup**, not warned about. |
| `SOLARIS_LOG_FORMAT` | `json` | Cloud Logging needs `severity`, not `level`. |
| `SOLARIS_DAILY_EE_CALL_BUDGET` | `5000` | The real quota guard. |
| `SOLARIS_GUEST_COMPUTATION_ALLOWANCE` | `10` | Comparing several roofs is the primary task, so this must exceed the number of sites a visitor will examine. |

Credentials resolve through plain `google.auth.default()`, which works
identically on Cloud Run, GCE, Cloud Build and under WIF. No `K_SERVICE`
environment sniffing.

## Quota protection, three layers

Cheapest first, because they defend against different things.

1. **Input bounds** (`schemas.py`). The only layer that helps against the worst
   case: one well-formed request for an oversized area. Runs before any `ee`
   object exists, so a rejected request costs nothing.
2. **A concurrency semaphore** (`limits.py`). An `anyio` request timeout frees
   the *client*, not the worker thread — `getInfo()` is uninterruptible blocking
   I/O. The semaphore is what actually bounds resource use.
3. **A daily Earth Engine call budget**, denominated in round-trips rather than
   HTTP requests. That is the difference that matters: a per-IP request limit
   does nothing about one client making expensive calls. Backed by a Firestore
   `Increment` so it is genuinely global across instances.

### The unit mismatch that matters

`SOLARIS_DAILY_EE_CALL_BUDGET` counts **round-trips**. Earth Engine bills
**EECU-seconds**. These are unrelated: one `/api/yield` over a yearly window is
12 round-trips but roughly 100 s of wall clock and substantial server-side
compute, while 12 round-trips over a single day is a small fraction of that.

So the in-app budget protects against a flood of requests. It does **not**
protect against a small number of expensive ones, and a budget of 5000 calls
can exhaust a monthly EECU allowance while reporting ample headroom.

**Set a daily EECU cap in the Earth Engine console** — Configuration → *Manage
quota limits*. That is the only guard denominated in the unit that actually
binds. Start low and raise it once real usage is visible on the same page.

Note also that the non-commercial **Community tier** carries no SLA and lower
concurrency limits than the commercial tiers. That is appropriate for a
portfolio deployment, but it is not a platform to point sustained traffic at.

If the shared counter is unreachable the local in-process budget still applies —
degrading from a global ceiling to a per-instance one, bounded at 2× by
`max-instances`. A cache that fails should stop caching; a budget that fails
must not stop limiting.

## Observability

Structured JSON logs, one line per request, carrying `status`, `duration_ms`,
`cache`, `ee_calls`, `identity_kind` and `request_id`.

**`ee_calls` is the most useful number in the system.** It is the quota
currency, and it turns "why is this slow or expensive" into a single log query.

Two log-based metrics and two alerts are enough: 5xx rate, and
budget-exceeded. Cloud Run already supplies request count, latency percentiles,
error rate and instance count for free.

Deliberately **no** Prometheus, `/metrics` endpoint, or OpenTelemetry. A scrape
endpoint needs something always-on to scrape it, which contradicts
scale-to-zero.

## Verifying a deployment

```bash
URL=$(gcloud run services describe solaris --region=$REGION --format='value(status.url)')

curl -fsS $URL/api/health
curl -fsS $URL/api/ready | jq .        # earth_engine must be "ok"
curl -fsS $URL/api/version | jq .

# A real computation. Run it twice: the second must report X-Cache: HIT.
curl -fsS -D- -X POST $URL/api/yield \
  -H 'content-type: application/json' \
  -d '{"lat":28.6139,"lon":77.2090,"baseline_mode":"yearly","year":2023}' \
  | tail -5
```

Check: `/api/ready` reports Earth Engine `ok`; the second identical request is a
cache hit; `data_quality.severity` is `ok`; and an error response contains a
`request_id` and **no** project id.

## Rollback

```bash
gcloud run revisions list --service=solaris --region=$REGION
gcloud run services update-traffic solaris --region=$REGION --to-revisions=REVISION=100
```

No staging environment and no canary traffic splitting — one URL, and
`update-traffic` is sufficient.

## Cost

With `min-instances=0`, Firestore's free tier, and the Artifact Registry free
allowance, a project at this traffic level sits at or near zero. The things that
would change that, in order: pinning a minimum instance (~$10–15/month),
Memorystore instead of Firestore (~$35/month plus a VPC connector), or a global
load balancer for Cloud Armor (~$18–25/month). All three were considered and
rejected; see the README.
