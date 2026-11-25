const tabList = document.getElementById("sample-tabs");
const tabs = Array.from(tabList.querySelectorAll('[role="tab"]'));

function selectTab(selected) {
  for (const tab of tabs) {
    const active = tab === selected;
    const panel = document.getElementById(tab.getAttribute("aria-controls"));
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    panel.hidden = !active;
  }
}

for (const [index, tab] of tabs.entries()) {
  const panel = document.getElementById(tab.getAttribute("aria-controls"));
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", tab.id);
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    const destinations = {
      ArrowLeft: (index + tabs.length - 1) % tabs.length,
      ArrowRight: (index + 1) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    };
    if (!Object.hasOwn(destinations, event.key)) return;
    event.preventDefault();
    const next = tabs[destinations[event.key]];
    selectTab(next);
    next.focus();
  });
}

selectTab(tabs[0]);
tabList.hidden = false;
