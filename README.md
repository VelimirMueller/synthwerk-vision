<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/banner/hero-v2-light.svg">
  <img alt="SYNTHWERK-VISION. It names a picture in 35 milliseconds. No cloud, no training. Working, v0.1.0." src="assets/banner/hero-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/lead-v2-light.svg">
  <img alt="synthwerk-vision gives one image a label and the label&#x27;s place in a topic tree (hut -&gt; building -&gt; building and structure). A label is a text, so a new label needs no training. It does one thing, on CPU, and does not want to talk about it." src="assets/readme/lead-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/stats-v2-light.svg">
  <img alt="93.1 % ROOT-TOPIC ACCURACY. 58 FROZEN EVAL PHOTOS. 35 ms PER IMAGE, CPU. 0 EXTERNAL API CALLS" src="assets/readme/stats-v2-dark.svg" width="100%">
</picture>
</p>

<br>

<p>
<img alt="01 WHAT IT DOES. A PICTURE IN. A LABEL OUT." src="assets/readme/divider-what-v2.svg" width="100%">
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/features-v2-light.svg">
  <img alt="OPEN VOCABULARY: A label is a text, so a new label needs no training. One more embedding, nothing else. TOPIC TREE: Every label has a place: hut -&gt; building -&gt; building and structure. RUNS ON CPU: ONNX Runtime, about 35 ms per image on an M-series Mac. No GPU, no external API" src="assets/readme/features-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/what-v2-light.svg">
  <img alt="Open vocabulary: A label is a text, so a new label needs no training or new weights. A topic tree: Every label has a place: beagle -&gt; dog -&gt; mammal -&gt; animal. Runs on CPU: ONNX Runtime, about 35 ms per image on an M-series Mac. No GPU, no external API. Eval gate: Scores 93.1 % root-topic accuracy on 58 frozen photos. CI fails below 90 %" src="assets/readme/what-v2-dark.svg" width="100%">
</picture>
</p>

<br>

<p>
<img alt="02 QUICK START. COPY. PASTE. CLASSIFY." src="assets/readme/divider-start-v2.svg" width="100%">
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/start-v2-light.svg">
  <img alt="Terminal: $ uv sync | $ uv run python scripts/fetch_model.py | $ uv run uvicorn synthwerk_vision.main:create_app --factory --reload | # open http://localhost:8000/docs | $ curl -F &quot;image=@hut.jpg&quot; http://localhost:8000/v1/classifications" src="assets/readme/start-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/setup-v2-light.svg">
  <img alt="uv sync: Install runtime + dev dependencies from uv.lock. uv run python scripts/fetch_model.py: Both backends, pinned + SHA-256 verified (~1.5 GB). Or one: fetch_model.py siglip or fetch_model.py mobilenet. uv run uvicorn synthwerk_vision.main:create_app --factory --reload: Start the API. Open http://localhost:8000/docs. curl -F &quot;image=@hut.jpg&quot; http://localhost:8000/v1/classifications: Classify one image" src="assets/readme/setup-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/startnotes-v2-light.svg">
  <img alt="Needs: uv (https://docs.astral.sh/uv/) and Python 3.12. First start: Embeds all label texts once (about 9 s on an M-series CPU) and caches them in models/label_cache/. Later starts take about 0.5 s" src="assets/readme/startnotes-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/docker-v2-light.svg">
  <img alt="Terminal: # SigLIP (default) | $ docker build -t synthwerk-vision . | # MobileNet: 14 MB of weights | $ docker build --build-arg BACKEND=mobilenet -t synthwerk-vision:small . | $ docker run -p 8000:8000 -v synthwerk-vision-uploads:/data/uploads synthwerk-vision" src="assets/readme/docker-v2-dark.svg" width="100%">
</picture>
</p>

<details>
<summary><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-7-v2-light.svg"><img alt="Plain text" src="assets/readme/links-7-v2-dark.svg" width="32%"></picture></summary>

```bash
uv sync                                  # install runtime + dev dependencies from uv.lock
uv run python scripts/fetch_model.py     # both backends, pinned + SHA-256 verified (~1.5 GB)
                                         # or: fetch_model.py siglip | fetch_model.py mobilenet
uv run uvicorn synthwerk_vision.main:create_app --factory --reload
```

```bash
curl -F "image=@hut.jpg" http://localhost:8000/v1/classifications
```

```bash
docker build -t synthwerk-vision .                                     # SigLIP (default)
docker build --build-arg BACKEND=mobilenet -t synthwerk-vision:small . # 14 MB of weights
docker run -p 8000:8000 -v synthwerk-vision-uploads:/data/uploads synthwerk-vision
```

</details>

<p align="center">
<a href="https://docs.astral.sh/uv/"><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-1-v2-light.svg"><img alt="uv (https://docs.astral.sh/uv/)" src="assets/readme/links-1-v2-dark.svg" width="32%"></picture></a>
<a href="http://localhost:8000/docs"><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-2-v2-light.svg"><img alt="API docs (http://localhost:8000/docs)" src="assets/readme/links-2-v2-dark.svg" width="32%"></picture></a>
<a href=".env.example"><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-3-v2-light.svg"><img alt="Settings (.env.example)" src="assets/readme/links-3-v2-dark.svg" width="32%"></picture></a>
</p>

<br>

<p>
<img alt="03 HOW IT WORKS. BOXES AND ARROWS, AS PROMISED." src="assets/readme/divider-how-v2.svg" width="100%">
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/flow-v2-light.svg">
  <img alt="IMAGE -&gt; MODEL -&gt; TAXONOMY -&gt; RESPONSE. The model scores every label. The taxonomy sums the scores into a topic path." src="assets/readme/flow-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/how-v2-light.svg">
  <img alt="Scores: The model (SigLIP 2, ONNX, fp32) scores every label. The score of a topic is the sum of its labels. Response: The best label, its topic path, the top-k labels and the topics. Uncertain: uncertain turns true when the root topic score is too low. Read it before you trust the answer" src="assets/readme/how-v2-dark.svg" width="100%">
</picture>
</p>

<br>

<p>
<img alt="04 USAGE. THE REFERENCE. CONDENSED." src="assets/readme/divider-usage-v2.svg" width="100%">
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/api-v2-light.svg">
  <img alt="GET /health: Liveness, and whether the model is loaded. POST /v1/classifications: Classify an image (not stored). POST /v1/images: Store an image, return its id. POST /v1/labels: Admin. Needs SYNTHWERK_VISION_ADMIN_TOKEN. GET /v1/labels: Admin. Needs SYNTHWERK_VISION_ADMIN_TOKEN. DELETE /v1/labels/{id}: Admin. Needs SYNTHWERK_VISION_ADMIN_TOKEN. POST /v1/feedback: Admin. Needs SYNTHWERK_VISION_ADMIN_TOKEN" src="assets/readme/api-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/backends-v2-light.svg">
  <img alt="siglip: Default. SigLIP 2 base/16, open vocabulary, ~1.5 GB of weights. mobilenet: MobileNetV2-12, 1000 ImageNet classes, 14 MB of weights" src="assets/readme/backends-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/config-v2-light.svg">
  <img alt="SYNTHWERK_VISION_: Every setting is an env var with this prefix. See .env.example. AURORAE_, AURORA_: Old prefixes. They are ignored. Rename them in every .env and deployment" src="assets/readme/config-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/reference-v2-light.svg">
  <img alt="Full reference: Every endpoint, response field, taxonomy rule, correction, env var, the dev tree and the deploy plan: docs/REFERENCE.md. Feature docs: The feature docs are in docs/features/" src="assets/readme/reference-v2-dark.svg" width="100%">
</picture>
</p>

<p align="center">
<a href="docs/REFERENCE.md"><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-4-v2-light.svg"><img alt="Reference (docs/REFERENCE.md)" src="assets/readme/links-4-v2-dark.svg" width="32%"></picture></a>
<a href="docs/features/"><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-5-v2-light.svg"><img alt="Feature docs (docs/features/)" src="assets/readme/links-5-v2-dark.svg" width="32%"></picture></a>
<a href="LICENSE"><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-6-v2-light.svg"><img alt="License (LICENSE)" src="assets/readme/links-6-v2-dark.svg" width="32%"></picture></a>
</p>

<br>

<p>
<img alt="05 STATUS. HONEST NUMBERS ONLY." src="assets/readme/divider-status-v2.svg" width="100%">
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/status-v2-light.svg">
  <img alt="Image classification: stable. Classification eval gate: stable. Image upload: beta. Runtime labels: beta. Picture corrections: beta. Deployment: none yet" src="assets/readme/status-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/now-v2-light.svg">
  <img alt="Now: Working, v0.1.0. Works: Classification, eval gate. Next: Deployment, contracts" src="assets/readme/now-v2-dark.svg" width="100%">
</picture>
</p>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/checks-v2-light.svg">
  <img alt="uv run pytest: Tests, coverage gate 90 %. uv run ruff check . &amp;&amp; uv run ruff format --check .: Lint and format. uv run mypy: Strict types. uv run python scripts/evaluate.py --min-topic-accuracy 0.9: Eval gate" src="assets/readme/checks-v2-dark.svg" width="100%">
</picture>
</p>

<details>
<summary><picture><source media="(prefers-color-scheme: light)" srcset="assets/readme/links-7-v2-light.svg"><img alt="Plain text" src="assets/readme/links-7-v2-dark.svg" width="32%"></picture></summary>

```bash
uv run pytest                                              # tests, coverage gate 90 %
uv run ruff check . && uv run ruff format --check .        # lint and format
uv run mypy                                                # strict types
uv run python scripts/evaluate.py --min-topic-accuracy 0.9 # eval gate
```

</details>

<p>
<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/readme/footer-v2-light.svg">
  <img alt="VM. studio / flagship · open source · look per vm-brand playbook · MIT (c) 2026 Velimir Mueller · -- EOF -- it knows what a hut is --" src="assets/readme/footer-v2-dark.svg" width="100%">
</picture>
</p>
