// Dev default. In the container this file is REGENERATED at start by
// frontend/docker-entrypoint.sh from BOT_API_BASE, which is why the API base is
// read at runtime instead of through REACT_APP_* -- CRA inlines those into the
// bundle at build time, so changing one would mean rebuilding the image.
window.__BOT_CONFIG__ = { apiBase: "http://127.0.0.1:8000" };
