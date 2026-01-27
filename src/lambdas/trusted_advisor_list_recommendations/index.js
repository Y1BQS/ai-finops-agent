const { pipeline } = require('stream/promises');
const { Readable } = require('stream');
const { TrustedAdvisorClient, ListRecommendationsCommand, GetRecommendationCommand } = require("@aws-sdk/client-trustedadvisor");

const client = new TrustedAdvisorClient({});
const CHUNK_SIZE = 25 * 1024; // 20KB

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

module.exports.handler = awslambda.streamifyResponse(async (event, responseStream ,context) => {
    try {
        console.log("Received event:", JSON.stringify(event, null, 2));
        // Set the content type to application/json
        responseStream.setContentType("application/json");
        
        const get_trusted_advisor_findings = async () => {
            const findings = [];
            
            try {
                // Get all cost optimization recommendations
                const input = {
                    maxResults: 200, // Adjust as needed
                    pillar: "cost_optimizing",
                    status: "warning", // Get recommendations that need attention
                };
                
                let hasMoreResults = true;
                let nextToken = undefined;
                
                while (hasMoreResults) {
                    if (nextToken) {
                        input.nextToken = nextToken;
                    }
                    
                    const command = new ListRecommendationsCommand(input);
                    const response = await client.send(command);
                    console.log("TA-Response:", JSON.stringify(response, null, 2));
                    for (const recommendation of response.recommendationSummaries) {
                        console.log("TA-Recommendation:", JSON.stringify(recommendation, null, 2));
                        try {
                            // Get detailed recommendation data
                            const detailCommand = new GetRecommendationCommand({
                                recommendationIdentifier: recommendation.arn
                            });
                            const detailResponse = await client.send(detailCommand);
                            
                            const costAggregates = recommendation.pillarSpecificAggregates?.costOptimizing || {};
                            const resourceAggregates = recommendation.resourcesAggregates || {};
                            
                            findings.push({
                                recommendationIdentifier: recommendation.arn,
                                checkName: recommendation.name,
                                checkId: recommendation.id,
                                status: recommendation.status,
                                description: detailResponse.recommendation?.description || '',
                                recommendedAction: detailResponse.recommendation?.recommendedActions?.[0]?.description || '',
                                resourceCount: resourceAggregates.errorCount + resourceAggregates.warningCount,
                                estimatedMonthlySavings: costAggregates.estimatedMonthlySavings || 0,
                                resources: detailResponse.recommendation?.resources?.map(resource => ({
                                    resourceId: resource.resourceId || '',
                                    region: resource.metadata?.region || '',
                                    status: resource.status,
                                    metadata: resource.metadata || {}
                                })) || []
                            });
                            
                        } catch (e) {
                            console.error(`Error processing recommendation ${recommendation.name}:`, e);
                        }
                    }
                    
                    nextToken = response.nextToken;
                    hasMoreResults = !!nextToken;
                }
                
                // Sort findings by estimated savings
                findings.sort((a, b) => (b.estimatedMonthlySavings || 0) - (a.estimatedMonthlySavings || 0));
                
                return findings;
                
            } catch (e) {
                console.error("Error retrieving recommendations:", e);
                throw e;
            }
        };

        // Get all findings
        const findings = await get_trusted_advisor_findings();
        
        // Calculate total savings
        const totalSavings = findings.reduce((sum, finding) => 
            sum + (finding.estimatedMonthlySavings || 0), 0);
        
        // Prepare the checks object
        const checks = {
            summary: {
                totalFindings: findings.length,
                totalPotentialSavings: Number(totalSavings.toFixed(2)),
                lastUpdated: new Date().toISOString().replace('T', ' ').substr(0, 19)
            },
            findings: findings
        };

        // Prepare response
        const response_body = {
            TEXT: {
                body: JSON.stringify({
                    checks: checks
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

