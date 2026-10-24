import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AboutSection } from "@/components/settings/AboutSection";

const mocks = vi.hoisted(() => ({
  install: vi.fn(),
  mode: vi.fn(),
  fallback: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  stop: vi.fn(),
  listener: undefined as
    | undefined
    | ((event: { payload: { stage: string } }) => void),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: async () => "4.0.20" }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: async (_: string, callback: typeof mocks.listener) => {
    mocks.listener = callback;
    return mocks.stop;
  },
}));
vi.mock("@/lib/api", () => ({
  settingsApi: {
    customUpdateMode: mocks.mode,
    installUpdateAndRestart: mocks.install,
    checkUpdates: mocks.fallback,
  },
}));
vi.mock("@/lib/toast", () => ({
  toast: { error: mocks.error, success: mocks.success },
}));
vi.mock("@/contexts/UpdateContext", () => ({
  useUpdate: () => ({
    hasUpdate: true,
    updateInfo: { availableVersion: "4.0.21" },
    checkUpdate: vi.fn(),
    resetDismiss: vi.fn(),
    isChecking: false,
  }),
}));
vi.mock("@/components/WhatsNewDialog", () => ({ WhatsNewDialog: () => null }));

describe("custom update button", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.mode.mockResolvedValue(true);
  });

  it("does not open the official updater when preserving customization fails", async () => {
    mocks.install.mockRejectedValue(
      new Error("patch conflict; current app kept"),
    );
    render(<AboutSection isPortable={false} />);
    await screen.findByText("settings.customUpdate.hint");
    fireEvent.click(screen.getByRole("button", { name: "settings.updateTo" }));
    await waitFor(() =>
      expect(mocks.error).toHaveBeenCalledWith(
        "settings.customUpdate.failed",
        expect.anything(),
      ),
    );
    expect(mocks.fallback).not.toHaveBeenCalled();
  });

  it("shows build progress and prevents a second click while updating", async () => {
    let finish!: (value: boolean) => void;
    mocks.install.mockImplementation(
      () =>
        new Promise<boolean>((resolve) => {
          finish = resolve;
        }),
    );
    const { unmount } = render(<AboutSection isPortable={false} />);
    await screen.findByText("settings.customUpdate.hint");
    fireEvent.click(screen.getByRole("button", { name: "settings.updateTo" }));
    await act(async () => {
      mocks.listener?.({ payload: { stage: "building" } });
    });
    expect(
      screen.getByRole("button", {
        name: "settings.customUpdate.stages.building",
      }),
    ).toBeDisabled();
    await act(async () => {
      finish(false);
    });
    expect(mocks.success).toHaveBeenCalledWith(
      "settings.upToDate",
      expect.anything(),
    );
    unmount();
    expect(mocks.stop).toHaveBeenCalled();
  });
});
