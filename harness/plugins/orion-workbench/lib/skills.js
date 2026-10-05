import { fileURLToPath } from "node:url";
import * as filesystemSkills from "@deepseek-ai/dsh-skill-filesystem";

export const name = "orion-workbench-skills";
export const inject = ["skills"];

// Resolve against the installed plugin, independent of Profile, cwd and backend.
export function apply(ctx) {
  ctx.plugin(filesystemSkills, {
    providerName: "orion-workbench-bundled",
    includeDefaultRoots: false,
    bundledSkillDir: fileURLToPath(new URL("../skills/", import.meta.url)),
    watch: false,
  });
}
