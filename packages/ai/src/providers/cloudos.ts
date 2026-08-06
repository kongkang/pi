import { openAICompletionsApi } from "../api/openai-completions.lazy.ts";
import { envApiKeyAuth } from "../auth/helpers.ts";
import { createProvider, type Provider } from "../models.ts";
import { CLOUDOS_MODELS } from "./cloudos.models.ts";

export function cloudosProvider(): Provider<"openai-completions"> {
	return createProvider({
		id: "cloudos",
		name: "CloudOS",
		baseUrl: "https://api-inference.cn.cloudos.com/v1",
		auth: { apiKey: envApiKeyAuth("CloudOS API key", ["CLOUDOS_API_KEY"]) },
		models: Object.values(CLOUDOS_MODELS),
		api: openAICompletionsApi(),
	});
}
