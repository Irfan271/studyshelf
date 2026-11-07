
document.addEventListener("DOMContentLoaded", function () {
    const picker = document.getElementById("subject-picker");

    if (!picker) return;

    const toggle = document.getElementById("subject-toggle");
    const dropdown = document.getElementById("subject-dropdown");
    const search = document.getElementById("subject-filter");
    const options = document.getElementById("subject-options");
    const selectedContainer = document.getElementById("selected-subjects");
    const placeholder = document.getElementById("subject-placeholder");
    const emptyMessage = document.getElementById("subject-empty");
    const otherField = document.getElementById("other-subject-field");
    const toggleIcon = document.getElementById("subject-toggle-icon");

    if (
        !toggle ||
        !dropdown ||
        !search ||
        !options ||
        !selectedContainer
    ) {
        console.error("StudyShelf subject picker: Required elements missing.");
        return;
    }

    const checkboxes = Array.from(
        options.querySelectorAll('input[type="checkbox"]')
    );

    function getLabel(checkbox) {
        const label = checkbox.closest(".subject-option");
        const text = label?.querySelector("span");

        return text ? text.textContent.trim() : checkbox.value;
    }

    function openDropdown() {
        dropdown.hidden = false;
        toggle.setAttribute("aria-expanded", "true");

        if (toggleIcon) {
            toggleIcon.textContent = "^";
        }
    }

    function closeDropdown() {
        dropdown.hidden = true;
        toggle.setAttribute("aria-expanded", "false");

        if (toggleIcon) {
            toggleIcon.textContent = "v";
        }
    }

    function updateOtherField() {
        const otherSelected = checkboxes.some(
            checkbox =>
                checkbox.checked &&
                checkbox.value === "Other"
        );

        if (otherField) {
            otherField.hidden = !otherSelected;
        }
    }

    function renderSelected() {
        selectedContainer
            .querySelectorAll(".selected-subject-tag")
            .forEach(tag => tag.remove());

        const selected = checkboxes.filter(
            checkbox => checkbox.checked
        );

        if (placeholder) {
            placeholder.hidden = selected.length > 0;
        }

        selected.forEach(checkbox => {
            const tag = document.createElement("span");
            tag.className = "selected-subject-tag";

            const text = document.createElement("span");
            text.textContent = getLabel(checkbox);

            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "remove-subject";
            remove.textContent = "x";
            remove.setAttribute(
                "aria-label",
                "Remove " + getLabel(checkbox)
            );

            remove.addEventListener("click", function (event) {
                event.preventDefault();
                event.stopPropagation();

                checkbox.checked = false;

                checkbox.dispatchEvent(
                    new Event("change", { bubbles: true })
                );
            });

            tag.appendChild(text);
            tag.appendChild(remove);

            selectedContainer.appendChild(tag);
        });

        updateOtherField();
    }

    function filterOptions() {
        const query = search.value.trim().toLowerCase();
        let visibleCount = 0;

        options.querySelectorAll(".subject-option").forEach(label => {
            const text = label.textContent.toLowerCase();
            const matches = text.includes(query);

            label.hidden = !matches;

            if (matches) {
                visibleCount++;
            }
        });

        if (emptyMessage) {
            emptyMessage.hidden = visibleCount > 0;
        }
    }

    // Use one delegated click handler for reliable dropdown interaction.
    document.addEventListener("click", function (event) {
        const clickedToggle = event.target.closest("#subject-toggle");

        if (clickedToggle && picker.contains(clickedToggle)) {
            event.preventDefault();
            event.stopPropagation();

            if (dropdown.hidden) {
                openDropdown();
                search.focus();
            } else {
                closeDropdown();
            }

            return;
        }

        // Keep the dropdown open when clicking inside the picker.
        if (picker.contains(event.target)) {
            return;
        }

        closeDropdown();
    });

    // Search subjects as the user types.
    search.addEventListener("input", filterOptions);

    // Update selected tags whenever a checkbox changes.
    checkboxes.forEach(checkbox => {
        checkbox.addEventListener("change", renderSelected);
    });

    // Escape closes the dropdown.
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !dropdown.hidden) {
            closeDropdown();
            toggle.focus();
        }
    });

    // Initial rendering for existing selections.
    renderSelected();
    filterOptions();
});
