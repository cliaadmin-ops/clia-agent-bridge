# UI/UX Overhaul Plan: CLIA Agent Switchboard (v2.0)

## 1. Aesthetic Direction: "Lake Vibe"
- **Color Palette:**
  - Primary: Deep Navy (`#1e3a8a`) - Trust, depth.
  - Secondary: Lake Teal (`#0d9488`) - Clarity, water.
  - Accent: Sunset Orange (`#f97316`) - Action, urgency (Rollbacks).
  - Background: Soft Slate (`#f8fafc`) - Clean, modern.
- **Typography:** Sans-serif (Inter or Roboto) for high readability.
- **Components:** Rounded corners (8px-12px), subtle drop shadows, and glassmorphism effects for the input area.

## 2. Chat Experience (The "Brain" Interface)
- **Markdown Support:**
  - Integrate `marked.js` to render Agent responses.
  - Support for bold, italics, lists, and code blocks (for technical snippets).
  - Agent should be instructed to use Markdown for all structured data.
- **WYSIWYG Input:**
  - Auto-expanding textarea.
  - Drag-and-drop file zone with visual previews (icons for PDF/Docx).
  - "Typing..." indicator when the background worker is active.
- **Message Bubbles:**
  - User: Right-aligned, Navy background, white text.
  - Agent: Left-aligned, White background, Navy border, Navy text.

## 3. Deployment & Action Cards
- **Unified Action Cards:**
  - Replace the current "Step 1/Step 2" text with a progress stepper (Visual line connecting dots).
  - **Staging Card:**
    - File path breadcrumbs.
    - Change summary in a clean list.
    - Buttons: [Confirm & Stage] (Primary), [Discard Plan] (Ghost/Outline).
  - **Success Card:**
    - High-fidelity status light (Pulsing states).
    - Buttons: [Approve & Push to Production] (Success Green), [Undo Last] (Warning Orange).
- **Redundancy Cleanup:**
  - Remove "Reset Dev" button. "Undo Last" is the primary recovery mechanism.
  - "Discard Plan" only appears *before* a push.

## 4. Technical Implementation (Frontend)
- **HTMX + Alpine.js:** Use Alpine.js for local UI state (modal toggles, file name display) and HTMX for server communication.
- **Markdown Rendering:**
  ```javascript
  // Example Alpine.js integration
  document.addEventListener('htmx:afterSwap', (evt) => {
      if (evt.detail.target.id === 'chat-history') {
          const lastMsg = evt.detail.target.lastElementChild;
          if (lastMsg.classList.contains('message-agent')) {
              lastMsg.innerHTML = marked.parse(lastMsg.innerHTML);
          }
      }
  });
  ```
- **Persistence:** Continue using `localStorage` but ensure `htmx.process()` is called on restoration to re-bind status listeners.

## 5. Agent Instructions Update
- Update system prompt to enforce Markdown formatting.
- Instruct Agent to provide "Win Themes" or "Impact Summaries" in the staging plan to help the user understand *why* the change is good.

## 6. Hand-off Requirements
- Advanced model should provide a single, production-ready `index.html` (including inline CSS/JS or links to CDN).
- Ensure mobile responsiveness for Site Managers checking updates on the go.

## 7. Known Issues & Backlog
- **Status Flicker:** Deployment status occasionally flashes "FAILED" for one polling cycle during the transition between revisions. (Do not prioritize, but track).
- **Success Gating:** The UI should only show the final "SUCCESS!" state once the "Live" status has been confirmed and remains stable for at least one polling cycle.
