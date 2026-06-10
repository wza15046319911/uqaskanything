# Code Style

## Formatting

Prettier (`.prettierrc`) and ESLint (`eslint.config.mjs`) are the source of truth for all formatting and linting rules. Follow them strictly — do not fight or override their output. Read the config files for specifics when needed.

## Functions

- Use **function declarations** for all exported/public functions — components, server actions, utilities, cached wrappers, etc.
- Arrow functions are only for **private/internal use**: event handlers, callbacks, inline functions inside a component or module.
- **Inside `useEffect`**: named functions must use function declarations, not arrow functions.
- **Naming**: Non-component functions use **camelCase** (`getArticleBySlug`, `handleClick`, `formatDate`). Components use **PascalCase** (covered in the Components section below).

  ```tsx
  // Do — exported functions use function declarations
  export function getArticleBySlugFromCache(slug: string) { ... }
  export default function Card({ children }: CardProps) { ... }

  // Do — private functions inside a component use arrow functions
  const handleClick = () => { ... };
  const formatDate = (date: Date) => date.toISOString();

  // Don't — exported functions should not be arrow functions
  export const getArticleBySlugFromCache = (slug: string) => { ... };
  export const Card = ({ children }: CardProps) => { ... };

  // Do — function declarations inside useEffect
  useEffect(() => {
    async function fetchData() { ... }
    fetchData();
  }, []);

  // Don't — arrow functions inside useEffect
  useEffect(() => {
    const fetchData = async () => { ... };
    fetchData();
  }, []);
  ```

## Components

- Always use **function declarations**, not arrow function variables (follows the rule above):
  ```tsx
  // Do
  function Card({ children }: CardProps) {}

  // Don't
  const Card = ({ children }: CardProps) => {};
  ```

- **Single component file** (only one component, or main + private helpers):
  Use `export default` on the main component. Private helpers are not exported.
  ```tsx
  function CardHeader({ title }: CardHeaderProps) {
    return <div>{title}</div>;
  }

  export default function Card({ children }: CardProps) {
    return <div><CardHeader title="..." />{children}</div>;
  }
  ```

- **Multiple shared components in one file**:
  Use named exports for all — no default export.
  ```tsx
  export function CardHeader({ title }: CardHeaderProps) {
    return <div>{title}</div>;
  }

  export function Card({ children }: CardProps) {
    return <div><CardHeader title="..." />{children}</div>;
  }
  ```

## File & Folder Naming

### `docs/` folder
- All `.md` files use `UPPERCASE_WITH_UNDERSCORE.md`
- Examples: `DEV_SCRIPTS.md`, `SERVER_ACTIONS.md`

### Component folders (`src/components/`, `src/templates/`, `src/layouts/`)
- **Folder names**: PascalCase (`Card`, `MainHome`, `Header`)
- **`.tsx` files**: PascalCase (`CardHeader.tsx`, `Featured.tsx`) — except `index.tsx`
- **`.ts` files**: kebab-case (`api-mapper-v2.ts`, `types.ts`)
- **Exception — `src/components/ui/`**: kebab-case for everything (shadcn convention, e.g., `dropdown-menu.tsx`, `badge.tsx`)

### Everything else (`src/lib/`, `src/utils/`, `src/server-actions/`, `src/app/`, etc.)
- kebab-case for both folders and files (`date-helper.ts`, `city-homepage/`)
- **Exception — `src/app/`**: Next.js routing conventions are allowed on top of kebab-case (`[slug]`, `(group)`, `@parallel`, `_private`)
