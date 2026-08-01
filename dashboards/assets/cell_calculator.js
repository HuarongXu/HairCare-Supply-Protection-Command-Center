/**
 * Cell Calculator Widget
 * Ctrl+Click on table cells to accumulate values and show their sum.
 * Works on Demand Data and Production Data tabs.
 */
(function () {
    'use strict';

    var selectedCells = [];
    var widget = null;

    function createWidget() {
        if (widget) return widget;
        widget = document.createElement('div');
        widget.id = 'cell-calc-widget';
        widget.style.cssText =
            'position:fixed;bottom:24px;right:24px;background:#1e293b;color:#f1f5f9;' +
            'padding:10px 16px;border-radius:10px;font-size:14px;z-index:9999;' +
            'box-shadow:0 4px 20px rgba(0,0,0,0.3);display:none;min-width:180px;' +
            'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;' +
            'transition:opacity 0.2s;user-select:none;';
        widget.innerHTML =
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
            '<span id="cell-calc-label" style="opacity:0.7;font-size:12px;">SUM</span>' +
            '<span id="cell-calc-value" style="font-weight:700;font-size:18px;">0</span>' +
            '<span style="opacity:0.4;font-size:14px;">|</span>' +
            '<span id="cell-calc-avg-label" style="opacity:0.7;font-size:12px;">AVG</span>' +
            '<span id="cell-calc-avg-value" style="font-weight:700;font-size:18px;">0</span>' +
            '<span id="cell-calc-count" style="opacity:0.6;font-size:12px;"></span>' +
            '<button id="cell-calc-clear" style="background:none;border:1px solid rgba(255,255,255,0.3);color:#f1f5f9;' +
            'border-radius:4px;cursor:pointer;padding:2px 8px;font-size:11px;margin-left:6px;" title="Clear selection">✕</button>' +
            '</div>';
        document.body.appendChild(widget);

        document.getElementById('cell-calc-clear').addEventListener('click', function (e) {
            e.stopPropagation();
            clearSelection();
        });
        return widget;
    }

    function parseNumber(text) {
        if (!text || text === '-' || text === '') return NaN;
        var cleaned = String(text).replace(/,/g, '').replace(/\s/g, '').replace(/%$/, '');
        var num = parseFloat(cleaned);
        return num;
    }

    function formatNumber(num) {
        if (isNaN(num)) return '0';
        if (Number.isInteger(num)) return num.toLocaleString();
        return num.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 1 });
    }

    function updateWidget() {
        var w = createWidget();
        if (selectedCells.length === 0) {
            w.style.display = 'none';
            return;
        }
        var sum = 0;
        var count = 0;
        selectedCells.forEach(function (c) {
            if (!isNaN(c.value)) {
                sum += c.value;
                count++;
            }
        });
        var avg = count > 0 ? sum / count : 0;
        document.getElementById('cell-calc-value').textContent = formatNumber(sum);
        document.getElementById('cell-calc-avg-value').textContent = formatNumber(avg);
        document.getElementById('cell-calc-count').textContent = count + ' cell' + (count !== 1 ? 's' : '');
        w.style.display = 'block';
    }

    function clearSelection() {
        selectedCells.forEach(function (c) {
            if (c.el) {
                c.el.style.outline = '';
                c.el.style.outlineOffset = '';
                c.el.style.backgroundColor = '';
            }
        });
        selectedCells = [];
        updateWidget();
    }

    function highlightCell(td, add) {
        if (add) {
            td.style.outline = '2px solid #3b82f6';
            td.style.outlineOffset = '-2px';
        } else {
            td.style.outline = '';
            td.style.outlineOffset = '';
        }
    }

    function isInTargetTab(el) {
        // Check if element is inside data-validation or production-data tab content
        var panel = el.closest('.tab-content, .tab-parent, [class*="summary-panel"]');
        var tabs = el.closest('.page');
        if (!tabs) return false;
        // Check active tab
        var activeTab = tabs.querySelector('.page-tab--active');
        if (!activeTab) return false;
        var tabLabel = activeTab.textContent.trim();
        return tabLabel === 'Demand Data' || tabLabel === 'Production Data';
    }

    document.addEventListener('click', function (e) {
        // Only process Ctrl+Click (or Cmd+Click on Mac)
        var td = e.target.closest('td.dash-cell');
        if (!td) return;

        if (!isInTargetTab(td)) return;

        var text = td.textContent.trim();
        var value = parseNumber(text);

        if (!e.ctrlKey && !e.metaKey) {
            // Normal click - start new selection
            clearSelection();
            if (!isNaN(value)) {
                selectedCells.push({ el: td, value: value, text: text });
                highlightCell(td, true);
                updateWidget();
            }
            return;
        }

        // Ctrl+Click - check if already selected
        var idx = -1;
        for (var i = 0; i < selectedCells.length; i++) {
            if (selectedCells[i].el === td) {
                idx = i;
                break;
            }
        }

        if (idx >= 0) {
            // Deselect
            highlightCell(td, false);
            selectedCells.splice(idx, 1);
        } else {
            // Add to selection
            if (!isNaN(value)) {
                selectedCells.push({ el: td, value: value, text: text });
                highlightCell(td, true);
            }
        }
        updateWidget();
    });

    // Clear selection when switching tabs
    var observer = new MutationObserver(function () {
        if (selectedCells.length > 0) {
            var anyVisible = selectedCells.some(function (c) {
                return c.el && c.el.offsetParent !== null;
            });
            if (!anyVisible) {
                clearSelection();
            }
        }
    });
    observer.observe(document.body, { childList: true, subtree: true });
})();
