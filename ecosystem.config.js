module.exports = {
  apps: [
    {
      name: "resume-api",
      script: "venv/bin/uvicorn",
      args: "app.api.routes:app --host 0.0.0.0 --port 8005",
      interpreter: "none",
      env: {
        NODE_ENV: "production",
      },
    },
    {
      // Concurrency is what makes the fan-out worth anything: beat queues one
      // `process_message` per email, and four of them are then extracted in
      // parallel. `--pool=solo` runs a single task at a time — it was why a
      // burst of resumes drained one after another while later beat ticks
      // reported "another cycle is already running".
      //
      // Each slot holds its own Mongo connection and can be running OCR on a
      // PDF, so the ceiling here is RAM rather than CPU. Four is comfortable on
      // a 2 GB VPS; measure a real batch before raising it.
      name: "resume-worker",
      script: "venv/bin/celery",
      args: "-A app.tasks.celery_app worker --loglevel=INFO --concurrency=4",
      interpreter: "none",
      // A resume can take minutes (OCR + LLM). Let the current task finish
      // before SIGKILL, or a restart mid-batch leaves messages half-processed.
      kill_timeout: 300000,
      env: {
        NODE_ENV: "production",
      },
  ],
};
