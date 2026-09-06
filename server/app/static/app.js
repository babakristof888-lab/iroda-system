/* A környezeti grafikon. Chart.js UMD build CDN-ről – nincs npm, nincs build step. */
(function () {
  "use strict";

  const canvas = document.getElementById("envChart");
  if (!canvas || typeof Chart === "undefined") {
    return;
  }

  const hours = canvas.dataset.hours || "24";
  const sensor = canvas.dataset.sensor || "default";
  const emptyNote = document.getElementById("envChartEmpty");

  const params = new URLSearchParams({ hours: hours, sensor_id: sensor });

  fetch("/api/v1/env/series?" + params.toString(), { credentials: "same-origin" })
    .then(function (response) {
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      return response.json();
    })
    .then(function (data) {
      const points = data.points || [];
      if (points.length === 0) {
        canvas.hidden = true;
        if (emptyNote) emptyNote.hidden = false;
        return;
      }

      const labels = points.map(function (p) {
        // "2026-09-06T08:00:00+02:00" -> "09-06 08:00"
        return p.ts.slice(5, 16).replace("T", " ");
      });

      new Chart(canvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Hőmérséklet (°C)",
              data: points.map(function (p) { return p.temp_avg; }),
              borderColor: "#dc3545",
              backgroundColor: "rgba(220,53,69,0.1)",
              yAxisID: "yTemp",
              tension: 0.3,
              spanGaps: true
            },
            {
              label: "Páratartalom (%)",
              data: points.map(function (p) { return p.hum_avg; }),
              borderColor: "#0d6efd",
              backgroundColor: "rgba(13,110,253,0.1)",
              yAxisID: "yHum",
              tension: 0.3,
              spanGaps: true
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            yTemp: { position: "left", title: { display: true, text: "°C" } },
            yHum: {
              position: "right",
              title: { display: true, text: "%" },
              grid: { drawOnChartArea: false }
            }
          }
        }
      });
    })
    .catch(function (err) {
      console.error("Nem sikerült betölteni a környezeti adatsort:", err);
      canvas.hidden = true;
      if (emptyNote) {
        emptyNote.textContent = "A grafikon adatai nem érhetők el.";
        emptyNote.hidden = false;
      }
    });
})();
