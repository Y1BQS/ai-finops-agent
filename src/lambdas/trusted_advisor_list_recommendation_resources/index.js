const { pipeline } = require('stream/promises');
const { Readable } = require('stream');
const { TrustedAdvisorClient, ListRecommendationResourcesCommand } = require("@aws-sdk/client-trustedadvisor");

const client = new TrustedAdvisorClient({});
const CHUNK_SIZE = 25 * 1024; // 25KB

const streamFunctionResponse = async (function_response, responseStream) => {
    const jsonResponse = JSON.stringify(function_response);
    const size = Buffer.byteLength(jsonResponse);
    console.log("function_response_size:", size / 1024);
    
    const requestStream = Readable.from(jsonResponse);

    for await (const chunk of requestStream) {
        console.log("chunk:", chunk.length / 1024);
        console.log("chunk_size:", CHUNK_SIZE / 1024);
        if (chunk.length > CHUNK_SIZE) {
            for (let i = 0; i < chunk.length; i += CHUNK_SIZE) {
                console.log("i:", i);
                await responseStream.write(chunk.slice(i, i + CHUNK_SIZE));
            }
        } else {
            await responseStream.write(chunk);
        }
    }

    responseStream.end();
}

module.exports.handler = awslambda.streamifyResponse(async (event, responseStream, context) => {
    try {
        console.log("Received event:", JSON.stringify(event, null, 2));
        responseStream.setContentType("application/json");

        // Extract recommendationIdentifier from parameters
        const recommendationIdentifier = event.parameters?.find(
            param => param.name === "recommendationIdentifier"
        )?.value;

        const get_resource_list = async (recommendationIdentifier) => {
            const resources = [];
            
            try {
                const input = {
                    maxResults: 200, // Adjust as needed
                    recommendationIdentifier: recommendationIdentifier,
                };
                
                let hasMoreResults = true;
                let nextToken = undefined;
                
                while (hasMoreResults) {
                    if (nextToken) {
                        input.nextToken = nextToken;
                    }
                    
                    const command = new ListRecommendationResourcesCommand(input);
                    const response = await client.send(command);
                    console.log("Resource-Response:", JSON.stringify(response, null, 2));

                    if (response.recommendationResourceSummaries) {
                        resources.push(...response.recommendationResourceSummaries.map(resource => ({
                            id: resource.id,
                            taResourceArn: resource.arn,
                            awsResourceId: resource.awsResourceId,
                            regionCode: resource.regionCode,
                            status: resource.status,
                            metadata: resource.metadata,
                            lastUpdatedAt: resource.lastUpdatedAt,
                            exclusionStatus: resource.exclusionStatus,
                            recommendationArn: resource.recommendationArn
                        })));
                    }
                    
                    nextToken = response.nextToken;
                    hasMoreResults = !!nextToken;
                }
                
                return resources;
                
            } catch (e) {
                console.error("Error retrieving resources:", e);
                throw e;
            }
        };
        
        if (!recommendationIdentifier) {
            throw new Error("recommendationIdentifier is required");
        }

        // Get all resources
        const resources = await get_resource_list(recommendationIdentifier);
        
        // Prepare response object
        const resourcesData = {
            summary: {
                totalResources: resources.length,
                lastUpdated: new Date().toISOString().replace('T', ' ').substr(0, 19)
            },
            resources: resources
        };

        // Prepare response
        const response_body = {
            TEXT: {
                body: JSON.stringify({
                    resourcesData: resourcesData
                })
            }
        };

        const function_response = {
            messageVersion: "1.0",
            response: {
                actionGroup: event.actionGroup || '',
                function: event.function,
                functionResponse: {
                    responseBody: response_body
                }
            },
        };

        await streamFunctionResponse(function_response, responseStream);

    } catch (e) {
        console.error("Unexpected error:", e);
        const error_response_body = {
            TEXT: {
                body: JSON.stringify({
                    error: e.message
                })
            }
        };

        const error_function_response = {
            messageVersion: "1.0",
            response: {
                actionGroup: event.actionGroup || '',
                function: event.function,
                responseState: 'REPROMPT',
                functionResponse: {
                    responseBody: error_response_body
                }
            }
        };

        await streamFunctionResponse(error_function_response, responseStream);
    }
});

