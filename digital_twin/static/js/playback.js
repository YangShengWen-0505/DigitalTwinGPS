import { fetchJson } from "./api.js";
import { contextLabel, dataUrls, isHistoryPage } from "./context.js";
import { $, extractTime, showToast } from "./ui.js";
import { renderRecords, renderRecordsIncremental, updateMarker } from "./mapView.js";

let allRecords = [];
let cursor = 0;
let streamId = null;
let followingLive = !isHistoryPage;
let isPlaying = false;
let playbackIndex = 0;
let playTimer = null;
let movementTimer = null;
let fetchInterval = 1000;
let lastErrorMessage = "";

function syncPlaybackUi() {
    const slider = $("timeSlider");
    slider.max = Math.max(0, allRecords.length - 1);
    slider.value = Math.min(playbackIndex, Math.max(0, allRecords.length - 1));
    $("playback-end-time").textContent = allRecords.length ? extractTime(allRecords.at(-1).time) : "--:--:--";
}

export function updatePlaybackState() {
    if (!allRecords.length) return;
    const record = allRecords[playbackIndex];
    $("timeSlider").value = playbackIndex;
    $("playback-time").textContent = extractTime(record.time);
    $("lat").textContent = record.lat.toFixed(6);
    $("lng").textContent = record.lng.toFixed(6);
    $("action").textContent = record.action;
    $("lastFix").textContent = extractTime(record.time);
    $("liveMode").textContent = isHistoryPage ? "HISTORY" : (followingLive ? "LIVE" : "PLAYBACK");
    $("liveBtn").classList.toggle("active", followingLive && !isHistoryPage);
    $("status-title-text").textContent = isHistoryPage ? contextLabel() : "Current Status (LIVE)";
    updateMarker(record, $("autoFollow").checked);
}

function resetRecords() {
    allRecords = [];
    cursor = 0;
    streamId = null;
    playbackIndex = 0;
    syncPlaybackUi();
    renderRecords([]);
}

export function togglePlay() {
    if (!allRecords.length) {
        showToast("No movement data is available for playback yet.", "error");
        return;
    }
    isPlaying = !isPlaying;
    $("playPauseBtn").innerHTML = isPlaying
        ? '<i class="fa-solid fa-pause"></i>'
        : '<i class="fa-solid fa-play"></i>';
    if (!isPlaying) {
        clearInterval(playTimer);
        return;
    }
    followingLive = false;
    if (playbackIndex >= allRecords.length - 1) playbackIndex = 0;
    playTimer = setInterval(() => {
        if (playbackIndex < allRecords.length - 1) {
            playbackIndex += 1;
            updatePlaybackState();
        } else {
            togglePlay();
        }
    }, 100);
}

async function loadMovementPage({ append, live }) {
    const page = await fetchJson(
        `${dataUrls.movements}?offset=${append ? cursor : 0}&limit=500`,
        "Unable to load movement data.",
    );
    if (streamId && page.stream_id !== streamId) resetRecords();
    streamId = page.stream_id;
    cursor = page.next_offset;
    const records = Array.isArray(page.records) ? page.records : [];
    if (!append) {
        allRecords = records;
        playbackIndex = Math.max(0, records.length - 1);
        renderRecords(allRecords);
    } else if (records.length) {
        allRecords.push(...records);
        renderRecordsIncremental(records, allRecords.length);
        if (live) playbackIndex = allRecords.length - 1;
    }
    syncPlaybackUi();
    if (allRecords.length) updatePlaybackState();
    return page;
}

function scheduleMovementFetch(delay = fetchInterval) {
    clearTimeout(movementTimer);
    if (!isHistoryPage) movementTimer = setTimeout(fetchLiveMovements, delay);
}

export async function fetchLiveMovements() {
    if (isHistoryPage) return;
    if (document.hidden) {
        scheduleMovementFetch(10000);
        return;
    }
    try {
        const page = await loadMovementPage({ append: true, live: true });
        fetchInterval = page.records?.length ? 1000 : Math.min(fetchInterval * 1.3, 5000);
        if (lastErrorMessage) showToast("Live movement connection restored.");
        lastErrorMessage = "";
        scheduleMovementFetch(page.has_more ? 0 : fetchInterval);
    } catch (error) {
        const message = error.message || "Unable to load live movement data.";
        if (message !== lastErrorMessage) showToast(message, "error");
        lastErrorMessage = message;
        fetchInterval = Math.min(fetchInterval * 2, 30000);
        scheduleMovementFetch(fetchInterval);
    }
}

export async function loadInitialMovements() {
    if (!isHistoryPage) {
        await fetchLiveMovements();
        return;
    }
    resetRecords();
    let page = await loadMovementPage({ append: false, live: false });
    while (page.has_more) {
        page = await loadMovementPage({ append: true, live: false });
        await new Promise((resolve) => window.setTimeout(resolve, 0));
    }
}

export function clearPlaybackData() {
    resetRecords();
}

export function bindPlaybackControls() {
    $("playPauseBtn").addEventListener("click", togglePlay);
    if (isHistoryPage) {
        $("liveBtn").textContent = "HISTORY";
        $("liveBtn").disabled = true;
        $("liveBtn").classList.remove("active");
    } else {
        $("liveBtn").addEventListener("click", () => {
            if (isPlaying) togglePlay();
            followingLive = true;
            playbackIndex = Math.max(0, allRecords.length - 1);
            updatePlaybackState();
            showToast("Returned to the latest live position.");
        });
    }
    $("timeSlider").addEventListener("input", (event) => {
        if (isPlaying) togglePlay();
        followingLive = false;
        playbackIndex = Number(event.target.value);
        updatePlaybackState();
    });
    $("showPoints").addEventListener("change", () => renderRecords(allRecords));
    $("typeFilter").addEventListener("change", () => renderRecords(allRecords));
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && !isHistoryPage) scheduleMovementFetch(0);
    });
}
