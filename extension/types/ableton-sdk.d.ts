// Declarations for the part of "@ableton-extensions/sdk" (API 1.0.0) this
// add-on uses, written from Ableton's public documentation so the code
// type-checks without the SDK, which is only distributed to beta testers.
// Like the real SDK, every class takes the API version (`Track<"1.0.0">`).
// With the SDK installed, `npm run typecheck:sdk` checks against the real one.

export type ApiVersion = "1.0.0";

export interface Handle {
  id: bigint;
}

export interface ArrangementSelection {
  time_selection_start: number;
  time_selection_end: number;
  selected_lanes: Handle[];
}

export interface ClipSlotSelection {
  selected_clip_slots: Handle[];
}

export type ContextMenuScope<V extends ApiVersion> = V extends "1.0.0"
  ? "AudioClip" | "AudioTrack" | "ClipSlot" | "DrumRack" | "MidiClip" | "MidiTrack" | "Sample"
    | "Scene" | "Simpler" | "ClipSlotSelection" | "AudioTrack.ArrangementSelection"
    | "MidiTrack.ArrangementSelection"
  : never;

export interface ActivationContext {
  hostApiVersion: string;
}

export declare class DataModelObject<Version extends ApiVersion> {
  get parent(): DataModelObject<Version> | null;
}

export declare class Clip<Version extends ApiVersion> extends DataModelObject<Version> {
  get name(): string;
  get startTime(): number;
  get endTime(): number;
  get duration(): number;
  get startMarker(): number;
  get endMarker(): number;
  get looping(): boolean;
  get loopStart(): number;
  get loopEnd(): number;
  get muted(): boolean;
}

export declare class AudioClip<Version extends ApiVersion> extends Clip<Version> {
  get filePath(): string;
  get warping(): boolean;
}

export interface NoteDescription {
  pitch: number;
  startTime: number;
  duration: number;
  velocity?: number;
  muted?: boolean;
}

export declare class MidiClip<Version extends ApiVersion> extends Clip<Version> {
  get notes(): NoteDescription[];
}

export declare class ClipSlot<Version extends ApiVersion> extends DataModelObject<Version> {
  get clip(): Clip<Version> | null;
}

export declare class TakeLane<Version extends ApiVersion> extends DataModelObject<Version> {
  get name(): string;
}

export declare class Track<Version extends ApiVersion> extends DataModelObject<Version> {
  get name(): string;
  get clipSlots(): ClipSlot<Version>[];
  get arrangementClips(): Clip<Version>[];
}

export declare class AudioTrack<Version extends ApiVersion> extends Track<Version> {}

export declare class MidiTrack<Version extends ApiVersion> extends Track<Version> {}

export declare class Song<Version extends ApiVersion> extends DataModelObject<Version> {
  get tracks(): Track<Version>[];
  get tempo(): number;
  get rootNote(): number;
  get scaleName(): string;
  get scaleMode(): boolean;
}

export declare class Application<Version extends ApiVersion> extends DataModelObject<Version> {
  get song(): Song<Version>;
}

export declare class Commands<Version extends ApiVersion> {
  registerCommand(commandId: string, callback: (...args: unknown[]) => void): void;
  executeCommand(commandId: string, ...args: unknown[]): void;
}

export declare class Environment<Version extends ApiVersion> {
  get storageDirectory(): string | undefined;
  get tempDirectory(): string | undefined;
  get language(): string | undefined;
}

export declare class Resources<Version extends ApiVersion> {
  renderPreFxAudio(track: AudioTrack<Version>, startTime: number, endTime: number): Promise<string>;
  importIntoProject(filePath: string): Promise<string>;
}

export declare class Ui<Version extends ApiVersion> {
  registerContextMenuAction(scope: ContextMenuScope<Version>, title: string,
                            commandId: string): Promise<() => Promise<void>>;
  showModalDialog(url: string, width: number, height: number): Promise<string>;
  withinProgressDialog(text: string, options: { progress?: number },
                       callback: (update: (updateText: string, progress?: number) => Promise<void>,
                                  abortSignal: AbortSignal) => Promise<unknown>): Promise<unknown>;
}

export interface ExtensionContext<Version extends ApiVersion> {
  application: Application<Version>;
  commands: Commands<Version>;
  environment: Environment<Version>;
  resources: Resources<Version>;
  ui: Ui<Version>;
  getObjectFromHandle<T extends DataModelObject<Version>>(
    handle: Handle, type: abstract new (...args: never) => T): T;
  withinTransaction<T>(fn: () => T): T;
}

export declare const initialize: <V extends ApiVersion>(context: ActivationContext,
                                                        apiVersion: V) => ExtensionContext<V>;
