/* A statisztika grafikonjai. Chart.js UMD CDN-ről – nincs npm, nincs build step.
 *
 * Két grafikon, mindkettő EGY adatsor (magnitúdó), ezért egyetlen alapszín.
 * A második szín státusz, nem sorozat: az automatikusan lezárt napokat jelöli,
 * és soha nem áll önmagában – jelmagyarázat és táblázatos nézet is kíséri.
 */
(function () {
  "use strict";

  if (typeof Chart === "undefined") {
    return;
  }

  /* A színeket a CSS-ből olvassuk, hogy egy helyen legyenek definiálva. */
  function szinek(elem) {
    const stilus = getComputedStyle(elem.closest(".viz-root") || document.body);
    const olvas = (nev, tartalek) => (stilus.getPropertyValue(nev) || tartalek).trim();
    return {
      series: olvas("--viz-series", "#2a78d6"),
      check: olvas("--viz-check", "#ec835a"),
      grid: olvas("--viz-grid", "#e1e0d9"),
      axis: olvas("--viz-axis", "#c3c2b7"),
      muted: olvas("--viz-muted", "#898781"),
      ink: olvas("--viz-ink", "#52514e"),
      surface: olvas("--viz-surface", "#ffffff")
    };
  }

  function oraSzoveg(orak) {
    const percek = Math.round(orak * 60);
    return Math.floor(percek / 60) + "ó " + String(percek % 60).padStart(2, "0") + "p";
  }

  function keres(utvonal) {
    return fetch(utvonal, { credentials: "same-origin" }).then(function (valasz) {
      if (!valasz.ok) {
        throw new Error("HTTP " + valasz.status);
      }
      return valasz.json();
    });
  }

  function hibaJelzes(canvas, uzenet) {
    canvas.hidden = true;
    const jelzes = document.createElement("p");
    jelzes.className = "small text-muted mb-0";
    jelzes.textContent = uzenet;
    canvas.parentNode.appendChild(jelzes);
  }

  /* Az oszlop végére írt érték. Csak a vízszintes grafikonon használjuk,
     ahol kevés sáv van -- napi bontásban minden oszlopra írni zaj lenne. */
  const vegErtekPlugin = {
    id: "vegErtek",
    afterDatasetsDraw: function (chart, args, opciok) {
      const ctx = chart.ctx;
      ctx.save();
      ctx.font = "600 12px system-ui, -apple-system, 'Segoe UI', sans-serif";
      ctx.fillStyle = opciok.color;
      ctx.textBaseline = "middle";
      chart.getDatasetMeta(0).data.forEach(function (sav, index) {
        const ertek = chart.data.datasets[0].data[index];
        if (!ertek) {
          return;
        }
        ctx.textAlign = "left";
        ctx.fillText(oraSzoveg(ertek), sav.x + 8, sav.y);
      });
      ctx.restore();
    }
  };

  /* ---------------- 1. Napi óraszám egy dolgozóra ---------------- */
  const napiCanvas = document.getElementById("dailyChart");
  if (napiCanvas) {
    const szin = szinek(napiCanvas);
    const parameterek = new URLSearchParams({
      employee_id: napiCanvas.dataset.employee,
      mode: napiCanvas.dataset.mode,
      anchor: napiCanvas.dataset.anchor
    });

    keres("/api/v1/stats/daily?" + parameterek.toString())
      .then(function (adat) {
        const sorok = adat.bars || [];
        if (sorok.length === 0) {
          hibaJelzes(napiCanvas, "Ebben az időszakban nincs adat.");
          return;
        }

        new Chart(napiCanvas, {
          type: "bar",
          data: {
            labels: sorok.map(function (sor) { return sor.label; }),
            datasets: [{
              label: "Ledolgozott óra",
              data: sorok.map(function (sor) { return sor.hours; }),
              backgroundColor: sorok.map(function (sor) {
                return sor.auto_closed ? szin.check : szin.series;
              }),
              maxBarThickness: 24,
              /* Az oszlop teteje lekerekített, az alapvonalnál szögletes. */
              borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
              borderSkipped: "bottom",
              categoryPercentage: 0.86,
              barPercentage: 0.9
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },  /* egy adatsor -- a cím megnevezi */
              tooltip: {
                callbacks: {
                  title: function (elemek) {
                    const sor = sorok[elemek[0].dataIndex];
                    return sor.date + " (" + sor.weekday + ")";
                  },
                  label: function (elem) {
                    const sor = sorok[elem.dataIndex];
                    if (sor.seconds === 0) {
                      return "nem volt bent";
                    }
                    const reszek = [oraSzoveg(sor.hours)];
                    reszek.push(sor.sessions + " munkamenet");
                    if (sor.auto_closed) { reszek.push("automatikus zárás – ellenőrzendő"); }
                    if (sor.open) { reszek.push("folyamatban"); }
                    return reszek.join(" · ");
                  }
                }
              }
            },
            scales: {
              x: {
                grid: { display: false },
                border: { color: szin.axis },
                ticks: { color: szin.muted, autoSkip: true, maxRotation: 0 }
              },
              y: {
                beginAtZero: true,
                title: { display: true, text: "óra", color: szin.ink },
                grid: { color: szin.grid, drawTicks: false },
                border: { display: false },
                ticks: { color: szin.muted, precision: 0 }
              }
            }
          }
        });
      })
      .catch(function (hiba) {
        console.error("Napi grafikon:", hiba);
        hibaJelzes(napiCanvas, "A grafikon adatai nem érhetők el.");
      });
  }

  /* ---------------- 2. Dolgozók összehasonlítása ---------------- */
  const osszeCanvas = document.getElementById("compareChart");
  if (osszeCanvas) {
    const szin = szinek(osszeCanvas);
    const parameterek = new URLSearchParams({
      mode: osszeCanvas.dataset.mode,
      anchor: osszeCanvas.dataset.anchor
    });

    keres("/api/v1/stats/employees?" + parameterek.toString())
      .then(function (adat) {
        const sorok = adat.employees || [];
        if (sorok.length === 0) {
          hibaJelzes(osszeCanvas, "Ebben az időszakban nincs adat.");
          return;
        }

        new Chart(osszeCanvas, {
          type: "bar",
          plugins: [vegErtekPlugin],
          data: {
            labels: sorok.map(function (sor) { return sor.name; }),
            datasets: [{
              label: "Ledolgozott óra",
              data: sorok.map(function (sor) { return sor.hours; }),
              backgroundColor: szin.series,
              maxBarThickness: 24,
              /* Vízszintes sáv: a jobb vége lekerekített, a bal (alapvonal) szögletes. */
              borderRadius: { topRight: 4, bottomRight: 4, topLeft: 0, bottomLeft: 0 },
              borderSkipped: "left",
              categoryPercentage: 0.8,
              barPercentage: 0.9
            }]
          },
          options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            layout: { padding: { right: 72 } },  /* hely a sáv végi értéknek */
            plugins: {
              legend: { display: false },
              vegErtek: { color: szin.ink },
              tooltip: {
                callbacks: {
                  label: function (elem) {
                    const sor = sorok[elem.dataIndex];
                    const reszek = [
                      oraSzoveg(sor.hours),
                      sor.days + " nap",
                      "napi átlag " + oraSzoveg(sor.average_hours_per_day)
                    ];
                    if (sor.auto_closed_days) {
                      reszek.push(sor.auto_closed_days + " nap ellenőrzendő");
                    }
                    if (sor.open_count) { reszek.push("folyamatban lévő munkamenet"); }
                    return reszek;
                  }
                }
              }
            },
            scales: {
              x: {
                beginAtZero: true,
                title: { display: true, text: "óra", color: szin.ink },
                grid: { color: szin.grid, drawTicks: false },
                border: { display: false },
                ticks: { color: szin.muted, precision: 0 }
              },
              y: {
                grid: { display: false },
                border: { color: szin.axis },
                ticks: { color: szin.ink }
              }
            }
          }
        });
      })
      .catch(function (hiba) {
        console.error("Összehasonlító grafikon:", hiba);
        hibaJelzes(osszeCanvas, "A grafikon adatai nem érhetők el.");
      });
  }
})();
