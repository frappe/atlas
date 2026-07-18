import { createApp } from "vue";
import "./style.css";
// Importing the theme module applies the persisted mode (system/light/dark) to
// <html> before the app mounts, so there's no flash of the wrong theme.
import "./theme.js";
// Importing the debug module seeds the alignment-borders switch from the URL
// (?borders) and applies the class to <html> before mount — no flash.
import "./debug.js";
import App from "./App.vue";

createApp(App).mount("#app");
