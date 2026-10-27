import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import Icon from "./Icon";
// 嵌套的模型管理弹窗独占焦点与 Escape，关闭后回到原任务草稿。
const modalStack: HTMLElement[] = [];
export default function Modal({
  title,
  drawer = false,
  children,
  onClose,
  initialFocusRef,
}: {
  title: string;
  drawer?: boolean;
  children: ReactNode;
  onClose: () => void;
  initialFocusRef?: RefObject<HTMLElement>;
}) {
  const ref = useRef<HTMLElement>(null),
    closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null,
      previousOverflow = document.body.style.overflow;
    const element = ref.current!;
    modalStack.push(element);
    document.body.style.overflow = "hidden";
    (initialFocusRef?.current ?? ref.current?.querySelector<HTMLButtonElement>("button"))?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || modalStack.at(-1) !== element) return;
      if (event.key === "Escape") closeRef.current();
      if (event.key !== "Tab") return;
      const items = Array.from(
        ref.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input, select, textarea, [tabindex="0"]',
        ) ?? [],
      ).filter(item => item.tabIndex >= 0 && !item.matches(":disabled"));
      const first = items[0],
        last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      modalStack.splice(modalStack.indexOf(element), 1);
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <div
      className="overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={ref}
        className={drawer ? "drawer" : "modal"}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onSubmit={event => event.stopPropagation()}
      >
        <button className="close-modal" onClick={onClose} aria-label="关闭">
          <Icon name="close" />
        </button>
        {children}
      </section>
    </div>
  );
}
