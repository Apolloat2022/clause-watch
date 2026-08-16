// ClauseWatch — deployment entry point.
//
// Subscription-scoped, which is what `azd` expects by default: azd creates the
// resource group itself and finds it again by the `azd-env-name` tag, so
// `azd down` removes the whole thing. Everything inside the group lives in
// modules/resources.bicep — resource-scoped resources cannot be declared
// directly in a subscription-scoped template.
//
// NOT YET DEPLOYED. `bicep build` passes; nothing here has been through a real
// `azd provision`. API versions in particular drift and are the most likely
// thing to need bumping.
//
// Cost posture (this is a personal Pay-As-You-Go subscription, see
// ARCHITECTURE.md §5):
//   * Cosmos is SERVERLESS — no provisioned RU/s floor billed continuously.
//   * No Azure AI Search. Vector search lives in Cosmos via DiskANN.
//   * Container Apps scale to zero; the jobs bill per execution.
//   * Container Registry Basic is the one unavoidable always-on line item,
//     roughly $5/month. There is no scale-to-zero registry tier.

targetScope = 'subscription'

@description('Name of the azd environment. Names the resource group and tags every resource with azd-env-name, which is how azd finds them again.')
@minLength(1)
@maxLength(64)
param environmentName string

@description('Primary location for all resources. azd supplies this from AZURE_LOCATION.')
@minLength(1)
param location string

@description('Object id of the developer or service principal running azd. Granted the same data-plane roles as the app identity, because local auth is disabled on Cosmos, Storage and Service Bus — without this there is no key to fall back on and a local run cannot read its own data. Empty skips those assignments.')
param principalId string = ''

@description('Whether principalId is a human or a service principal. azd sets AZURE_PRINCIPAL_TYPE; CI pipelines need ServicePrincipal.')
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'

@description('Short name used to derive resource names. Lowercase alphanumeric.')
@minLength(3)
@maxLength(12)
param appName string = 'clausewatch'

@description('Value of ENVIRONMENT inside the containers. Deliberately separate from environmentName: app/config.py constrains this to local/dev/prod, while an azd environment may be called anything.')
@allowed(['dev', 'prod'])
param appEnvironment string = 'dev'

@description('Resource group name. Defaults to the azd convention, rg-<environmentName>.')
param resourceGroupName string = ''

@description('Embedding dimensions — must match config.embedding_dimensions.')
param embeddingDimensions int = 1536

@description('Fully-qualified image for the API and both jobs. azd sets SERVICE_API_IMAGE_NAME during `azd deploy`; main.parameters.json threads it back here so every later provision pins all three containers to the image azd actually built. Empty only on the very first provision, before any image exists.')
param containerImage string = ''

@description('Foundry resource name — Claude for extraction. Not provisioned here: Foundry model access is granted per-subscription out of band, so this is supplied rather than created.')
param foundryResource string = ''

@description('Azure OpenAI endpoint serving the embedding deployment. Usually the same Foundry resource on its Azure OpenAI hostname.')
param azureOpenAiEndpoint string = ''

@description('Extraction prompt version, stored on every obligation so a bad batch is findable and re-runnable. Bump when the prompt changes.')
param promptVersion string = 'v1'

@description('Incoming-webhook URL (Teams / Slack) for the daily scan summary. Secret — it is a bearer credential in URL form — so it is injected as a container secret, not a plain env var. Empty leaves the scanner logging only.')
@secure()
param notifyWebhookUrl string = ''

@description('Entra ID tenant id for API bearer validation. LEAVING THIS EMPTY DEPLOYS AN UNAUTHENTICATED API: app/auth.py only installs the middleware when both this and entraAudience are set, and the ingress below is external. Optional solely so a first `azd up` works before an app registration exists.')
param entraTenantId string = ''

@description('Application ID URI of the app registration this API accepts tokens for, e.g. api://clausewatch. Must be set together with entraTenantId.')
param entraAudience string = ''

var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: !empty(resourceGroupName) ? resourceGroupName : 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'modules/resources.bicep' = {
  name: 'clausewatch-resources'
  scope: rg
  params: {
    location: location
    tags: tags
    appName: appName
    appEnvironment: appEnvironment
    environmentName: environmentName
    embeddingDimensions: embeddingDimensions
    containerImage: containerImage
    foundryResource: foundryResource
    azureOpenAiEndpoint: azureOpenAiEndpoint
    promptVersion: promptVersion
    notifyWebhookUrl: notifyWebhookUrl
    entraTenantId: entraTenantId
    entraAudience: entraAudience
    principalId: principalId
    principalType: principalType
  }
}

// ------------------------------------------------------------------ outputs
// azd reads these off the deployment and writes them into the azd environment,
// where `.env.example` says they come from. The AZURE_CONTAINER_REGISTRY_*
// pair is not optional: it is how `azd deploy` knows where to push.
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.AZURE_CONTAINER_REGISTRY_NAME
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = resources.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_ID
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = resources.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_NAME

output SERVICE_API_NAME string = resources.outputs.SERVICE_API_NAME
output SERVICE_API_ENDPOINT_URL string = resources.outputs.SERVICE_API_ENDPOINT_URL

output AZURE_CLIENT_ID string = resources.outputs.AZURE_CLIENT_ID
output COSMOS_ENDPOINT string = resources.outputs.COSMOS_ENDPOINT
output COSMOS_DATABASE string = resources.outputs.COSMOS_DATABASE
output STORAGE_ACCOUNT_URL string = resources.outputs.STORAGE_ACCOUNT_URL
output SERVICEBUS_NAMESPACE string = resources.outputs.SERVICEBUS_NAMESPACE
output DOC_INTELLIGENCE_ENDPOINT string = resources.outputs.DOC_INTELLIGENCE_ENDPOINT
