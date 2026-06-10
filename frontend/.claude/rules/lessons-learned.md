# Lessons Learned — Frontend

Anti-patterns and practices learned from past mistakes. Follow these to avoid
repeating errors during frontend work.

## Only Implement What Is Requested

**Problem**: Over-engineering by adding extra props, abstractions, or
"improvements" that were not asked for.

**Solution**: Stick to the requirements. Do not anticipate future needs.

**Avoid**:

- Adding customizable props when hardcoded values are enough
- Config options that weren't requested
- Error handling for cases that won't happen
- Callbacks like `onClose`/`onChange` when nothing uses them
- Making a component "flexible" when it serves one purpose

**Key points**:

- Ask "Did the user ask for this?" — if no, don't add it.
- Simple, focused code beats flexible, complex code.
- If flexibility is needed later, add it then.

## Gather Context Yourself — Don't Ask the User About Code

**Problem**: Asking the user to confirm things that the code already answers
("Is this type used elsewhere?", "Does the API return this field?").

**Solution**: Read the file, grep for usages, follow imports. For the shape of
an API response, check the backend (`backend/app/api/`, `backend/app/services/`)
or call the endpoint — don't ask. Only ask the user about **intent, priorities,
or genuinely ambiguous requirements**.

**Example**:

```
// DON'T: "Does /api/sim/state return a `validation` field?"
// DO:    open backend/app/api/sim.py, read the sim_state return dict,
//        then type the response accordingly.
```

**Key points**:

- Curiosity about code is your job, not the user's burden.
- Read → Grep → follow imports → keep digging until an answer or a real ambiguity.
- Surface what you found and what it means; don't dump raw tool output.

## Ask for Clarification Instead of Assuming

**Problem**: Guessing the meaning of an ambiguous request instead of asking.

**Solution**: When a term could mean several things, or your interpretation
would shift the topic, ask one short clarifying question first. This is about
ambiguous user **intent** — distinct from the code-facts rule above.

**Key points**:

- Stay in the context of the conversation.
- A quick question prevents wasted work.
- When uncertain about intent, ask — don't guess.

## Reusable Components Own Their Own Types

**Problem**: A reusable component (layout, UI primitive) imports types from a
specific page/feature, creating a reverse dependency.

**Solution**: Each reusable component defines its own type from what IT needs.
The consumer transforms its data to match.

```typescript
// DON'T — shared component depends on a feature
import { Course } from '../pages/Ask/types' // wrong direction

// DO — component owns its type
export interface CourseRowProps {
  code: string
  title: string
}
```

**Key points**:

- UI components must NEVER import from pages/features.
- Dependency direction: page → component → primitive (not the reverse).
- Each component defines only the fields it actually uses.

## Reuse Before Writing Custom UI

**Problem**: Hand-writing markup/styles for something that already exists as a
component.

**Solution**: Before adding custom UI, check `src/components/` for something to
compose. This project has no component library yet; if one is added later
(e.g. shadcn/ui), prefer its components and variants over hand-rolled styles.

**Key points**:

- Composing existing components keeps the UI consistent.
- A change to a shared component then propagates everywhere.

## Keep Design Values Centralized

**Problem**: Scattering the same color/spacing/size as hardcoded values across
many files.

**Solution**: Centralize design values (a shared CSS file, CSS variables, or a
theme object). The project currently uses plain CSS / inline styles; if CSS
variables or Tailwind are introduced, use tokens — not arbitrary values like
`text-[#333]` or `rounded-[22.5px]`.

**Key points**:

- Define a value once; reference it everywhere.
- Hardcoded values drift apart; centralized ones update in one place.

## Verify Every Usage Before Removing or Renaming Anything

**Problem**: Removing/renaming a state, prop, function, type, or export based on
a partial search, then breaking consumers that were missed.

**Solution**: Search for **every** usage across the codebase and read each
result before removing or renaming. "Grep and skim" is not verification.

**Key points**:

- Applies to everything: state, props, functions, types, exports, CSS classes.
- Read each result — don't assume "only used in X" from a few matches.
- When many consumers exist, check them all; when in doubt, list what you
  checked and let the user confirm.
