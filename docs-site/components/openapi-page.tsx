"use client";
import { createOpenAPIPage } from "fumadocs-openapi/ui";

// createOpenAPIPage is a client factory (it wires up the interactive schema
// viewer), so it must be constructed inside a client module. The generated
// content/api MDX pulls this in through the MDX component map.
export const OpenAPIPage = createOpenAPIPage();
