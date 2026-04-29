/**
 * Promise-returning modal dialog helpers built on the native
 * <dialog> element. Used in place of window.confirm / window.alert,
 * which are blocking modals the browser owns and which break browser
 * automation (Playwright/Cypress/Selenium have to register a `dialog`
 * handler ahead of time, and if you forget the test hangs). The
 * native <dialog> element is just a DOM node — automation tools see
 * its buttons and can click them like anything else.
 */

interface ConfirmOptions {
    title?: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    /** Style the confirm button as destructive (red). Default false. */
    destructive?: boolean;
}

interface AlertOptions {
    title?: string;
    message: string;
    confirmLabel?: string;
}

/** Show a modal yes/no confirmation. Resolves to true if the user
 * clicks confirm, false if they cancel or dismiss. */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
    return new Promise((resolve) => {
        const dlg = _buildDialog({
            title: opts.title,
            message: opts.message,
            buttons: [
                {
                    label: opts.cancelLabel ?? 'Cancel',
                    role: 'cancel',
                    onClick: () => resolve(false),
                },
                {
                    label: opts.confirmLabel ?? 'OK',
                    role: 'confirm',
                    destructive: opts.destructive ?? false,
                    onClick: () => resolve(true),
                },
            ],
            onDismiss: () => resolve(false),
        });
        document.body.appendChild(dlg);
        dlg.showModal();
    });
}

/** Show a modal informational/error message with one OK button.
 * Resolves when dismissed. */
export function alertDialog(opts: AlertOptions): Promise<void> {
    return new Promise((resolve) => {
        const dlg = _buildDialog({
            title: opts.title,
            message: opts.message,
            buttons: [
                {
                    label: opts.confirmLabel ?? 'OK',
                    role: 'confirm',
                    onClick: () => resolve(),
                },
            ],
            onDismiss: () => resolve(),
        });
        document.body.appendChild(dlg);
        dlg.showModal();
    });
}

interface _ButtonSpec {
    label: string;
    role: 'confirm' | 'cancel';
    destructive?: boolean;
    onClick: () => void;
}

interface _DialogSpec {
    title?: string;
    message: string;
    buttons: _ButtonSpec[];
    onDismiss: () => void;
}

function _buildDialog(spec: _DialogSpec): HTMLDialogElement {
    const dlg = document.createElement('dialog');
    dlg.className = 'app-dialog';
    // Cancel via Esc / backdrop click resolves with the dismiss action.
    let resolved = false;
    dlg.addEventListener('close', () => {
        if (!resolved) {
            resolved = true;
            spec.onDismiss();
        }
        dlg.remove();
    });

    if (spec.title) {
        const h = document.createElement('h3');
        h.className = 'app-dialog-title';
        h.textContent = spec.title;
        dlg.appendChild(h);
    }

    const body = document.createElement('p');
    body.className = 'app-dialog-message';
    body.textContent = spec.message;
    dlg.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'app-dialog-actions';

    for (const btn of spec.buttons) {
        const el = document.createElement('button');
        el.type = 'button';
        el.textContent = btn.label;
        el.name = btn.role;  // automation can locator('dialog button[name="confirm"]')
        el.className = `app-dialog-btn app-dialog-btn-${btn.role}`;
        if (btn.destructive) el.classList.add('app-dialog-btn-destructive');
        el.addEventListener('click', () => {
            if (resolved) return;
            resolved = true;
            btn.onClick();
            dlg.close();
        });
        actions.appendChild(el);
    }

    dlg.appendChild(actions);
    return dlg;
}
