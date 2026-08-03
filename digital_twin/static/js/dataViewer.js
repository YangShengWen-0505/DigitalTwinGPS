import { apiFetch, fetchJson } from "./api.js";
import { contextLabel, dataUrls } from "./context.js";
import { $, escapeHtml, openModal, renderError } from "./ui.js";

let csvOffsets = [0];
let csvPageIndex = 0;

function setTitle(value) {
    $("data-modal-title").textContent = `${value} · ${contextLabel()}`;
}

function renderCsvPage(page) {
    const body = $("data-body");
    const records = Array.isArray(page.records) ? page.records : [];
    const rows = records.map((record) => `
        <tr>
            <td>${Number(record.sequence)}</td>
            <td>${escapeHtml(record.time)}</td>
            <td>${Number(record.lat).toFixed(7)}</td>
            <td>${Number(record.lng).toFixed(7)}</td>
            <td>${escapeHtml(record.action)}</td>
            <td>${escapeHtml(record.note || "")}</td>
        </tr>
    `).join("");
    body.innerHTML = `
        <div class="csv-table-wrap">
            <table class="csv-table">
                <thead><tr><th>Sequence</th><th>Timestamp (UTC)</th><th>Latitude</th><th>Longitude</th><th>Action</th><th>Note</th></tr></thead>
                <tbody>${rows || '<tr><td colspan="6">This session has no movement records.</td></tr>'}</tbody>
            </table>
        </div>
        <div class="csv-pager">
            <button class="action-btn" id="csvPrev" ${csvPageIndex === 0 ? "disabled" : ""}>Previous</button>
            <span>Page ${csvPageIndex + 1}</span>
            <button class="action-btn" id="csvNext" ${page.has_more ? "" : "disabled"}>Next</button>
        </div>
    `;
    $("csvPrev").addEventListener("click", () => loadCsvPage(csvPageIndex - 1));
    $("csvNext").addEventListener("click", () => {
        if (!csvOffsets[csvPageIndex + 1]) csvOffsets[csvPageIndex + 1] = page.next_offset;
        loadCsvPage(csvPageIndex + 1);
    });
}

async function loadCsvPage(index) {
    if (index < 0) return;
    csvPageIndex = index;
    $("data-body").innerHTML = '<div class="empty-state">Loading movement records...</div>';
    try {
        const page = await fetchJson(
            `${dataUrls.movements}?offset=${csvOffsets[index] || 0}&limit=250`,
            `Unable to load movement records for ${contextLabel()}.`,
        );
        renderCsvPage(page);
    } catch (error) {
        renderError($("data-body"), error.message || "Unable to load movement records.");
    }
}

export function openCsvViewer() {
    csvOffsets = [0];
    csvPageIndex = 0;
    setTitle("CSV contents");
    openModal("data-modal");
    loadCsvPage(0);
}

export async function openLogViewer(logName) {
    setTitle(`${logName.charAt(0).toUpperCase()}${logName.slice(1)} log`);
    openModal("data-modal");
    $("data-body").innerHTML = '<div class="empty-state">Loading log...</div>';
    try {
        const response = await apiFetch(dataUrls.log(logName));
        const text = await response.text();
        if (!response.ok) {
            $("data-body").innerHTML = `<div class="empty-state">${escapeHtml(text || "This session has no data for this log.")}</div>`;
            return;
        }
        $("data-body").innerHTML = `<pre class="log-viewer">${escapeHtml(text || "This session has no data for this log.")}</pre>`;
    } catch (error) {
        renderError($("data-body"), error.message || "Unable to load log data.");
    }
}
