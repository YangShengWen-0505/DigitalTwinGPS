export async function apiFetch(url, options = {}) {
    const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
    });

    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Your session has expired. Please sign in again.");
    }
    return response;
}

export async function fetchJson(url, fallbackMessage) {
    const response = await apiFetch(url);
    if (!response.ok) {
        throw new Error(fallbackMessage || `Request failed with HTTP ${response.status}.`);
    }
    return response.json();
}
