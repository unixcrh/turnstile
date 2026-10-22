targetScope = 'resourceGroup'

param apimName string
param observerKeyNamedValue string
param publisherPrincipalId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource observerKey 'Microsoft.ApiManagement/service/namedValues@2024-05-01' existing = {
  parent: apim
  name: observerKeyNamedValue
}

resource observerKeyReader 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(observerKey.id, 'turnstile-publisher-observer-key-reader')
  properties: {
    roleName: 'Turnstile Publisher Observer Key Reader ${uniqueString(observerKey.id)}'
    description: 'Reads the designated Observer key for authenticated Publisher capability checks.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/service/namedValues/listValue/action'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource publisherObserverKeyReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(observerKey.id, publisherPrincipalId, observerKeyReader.id)
  scope: observerKey
  properties: {
    principalId: publisherPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: observerKeyReader.id
  }
}

output observerKeyScope string = observerKey.id
output roleDefinitionId string = observerKeyReader.id
