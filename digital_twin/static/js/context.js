const node = document.getElementById("page-context");

export const pageContext = node ? JSON.parse(node.textContent) : { mode: "live" };
export const isHistoryPage = pageContext.mode === "history";

const historyBase = isHistoryPage
    ? `/api/history/${encodeURIComponent(pageContext.date)}/${encodeURIComponent(pageContext.session)}`
    : "";

export const dataUrls = {
    status: isHistoryPage ? `${historyBase}/status` : "/api/system_status",
    route: isHistoryPage ? `${historyBase}/planned_route` : "/api/planned_route",
    navigation: isHistoryPage ? `${historyBase}/navigation` : "/api/navigation_history",
    movements: isHistoryPage ? `${historyBase}/movements` : "/api/movements/current",
    log: (name) => isHistoryPage ? `${historyBase}/log/${encodeURIComponent(name)}` : `/api/log/${encodeURIComponent(name)}`,
};

export function contextLabel() {
    return isHistoryPage ? `HISTORY · ${pageContext.id}` : "LIVE";
}
