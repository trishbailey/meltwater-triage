/* Put a finished Excel export in the sidebar list without reloading the page. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.mtRecentExports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var LIMIT = 10;

  function safeId(id) {
    id = String(id || "");
    return /^[A-Za-z0-9_-]{1,64}$/.test(id) ? id : "";
  }

  function labelFor(job) {
    var name = job && job.filenames != null ? String(job.filenames) : "";
    return name ? name : "Flagged posts";
  }

  function shouldList(job) {
    return !!(job && job.state === "done" && job.has_download);
  }

  function noteCompleted(jobId, job) {
    var id = safeId(jobId);
    if (!id || !shouldList(job) || typeof document === "undefined") return false;
    var card = document.getElementById("recent-downloads");
    if (!card) return false;
    var list = card.querySelector("ul.dl-list");
    if (!list) return false;
    var href = "/jobs/" + id + "/download";
    var items = list.querySelectorAll("li");
    for (var i = 0; i < items.length; i++) {
      var existing = items[i];
      var link = existing.querySelector("a");
      if (existing.getAttribute("data-job") === id || (link && link.getAttribute("href") === href)) {
        existing.setAttribute("data-job", id);
        card.hidden = false;
        return false;
      }
    }
    var li = document.createElement("li");
    li.setAttribute("data-job", id);
    var a = document.createElement("a");
    a.setAttribute("href", href);
    a.textContent = labelFor(job);
    li.appendChild(a);
    if (list.firstChild) list.insertBefore(li, list.firstChild);
    else list.appendChild(li);
    while (list.children.length > LIMIT && list.lastElementChild) {
      list.removeChild(list.lastElementChild);
    }
    card.hidden = false;
    return true;
  }

  return {
    LIMIT: LIMIT,
    labelFor: labelFor,
    noteCompleted: noteCompleted,
    safeId: safeId,
    shouldList: shouldList
  };
});
