/* Corner box that spells out abbreviations the mentor uses. Add terms to G as needed. */
(() => {
  const G = {
    AWS: "Amazon Web Services", SCP: "Service Control Policy", OU: "Organizational Unit", IAM: "Identity and Access Management",
    VPC: "Virtual Private Cloud", EC2: "Elastic Compute Cloud", S3: "Simple Storage Service", EBS: "Elastic Block Store",
    EFS: "Elastic File System", RDS: "Relational Database Service", SNS: "Simple Notification Service", SQS: "Simple Queue Service",
    ALB: "Application Load Balancer", NLB: "Network Load Balancer", ELB: "Elastic Load Balancing", ASG: "Auto Scaling Group",
    AZ: "Availability Zone", KMS: "Key Management Service", SSO: "Single Sign-On", SAML: "Security Assertion Markup Language",
    ARN: "Amazon Resource Name", CLI: "Command Line Interface", SDK: "Software Development Kit", API: "Application Programming Interface",
    CDN: "Content Delivery Network", DNS: "Domain Name System", TTL: "Time To Live", NAT: "Network Address Translation",
    IGW: "Internet Gateway", TGW: "Transit Gateway", VPN: "Virtual Private Network", CIDR: "Classless Inter-Domain Routing",
    NACL: "Network Access Control List", WAF: "Web Application Firewall", DDOS: "Distributed Denial of Service",
    RAM: "Resource Access Manager", RPO: "Recovery Point Objective", RTO: "Recovery Time Objective", DR: "Disaster Recovery",
    HA: "High Availability", SLA: "Service Level Agreement", IaC: "Infrastructure as Code", CFN: "CloudFormation", CDK: "Cloud Development Kit",
    ECS: "Elastic Container Service", EKS: "Elastic Kubernetes Service", ECR: "Elastic Container Registry", SSM: "Systems Manager",
    DMS: "Database Migration Service", SCT: "Schema Conversion Tool", CMK: "Customer Managed Key", MFA: "Multi-Factor Authentication",
    GDPR: "General Data Protection Regulation", HIPAA: "Health Insurance Portability and Accountability Act", PCI: "Payment Card Industry",
    SAP: "Solutions Architect Professional", COGS: "Cost of Goods Sold", ROI: "Return on Investment", NPV: "Net Present Value",
    IRR: "Internal Rate of Return", CAGR: "Compound Annual Growth Rate", CAPM: "Capital Asset Pricing Model", WACC: "Weighted Average Cost of Capital",
    EBITDA: "Earnings Before Interest, Taxes, Depreciation and Amortization", LSAT: "Law School Admission Test", LR: "Logical Reasoning",
    RC: "Reading Comprehension", LG: "Logic Games", CSV: "Comma-Separated Values", JSON: "JavaScript Object Notation", HTTP: "Hypertext Transfer Protocol",
    HTTPS: "HTTP Secure", TLS: "Transport Layer Security", SSL: "Secure Sockets Layer", TCP: "Transmission Control Protocol", IP: "Internet Protocol",
    CPU: "Central Processing Unit", GPU: "Graphics Processing Unit", IOPS: "Input/Output Operations Per Second", SSD: "Solid-State Drive",
  };
  const box = document.createElement("aside");
  box.id = "gloss"; box.setAttribute("aria-label", "Abbreviations"); box.hidden = true;
  document.querySelector(".stage").appendChild(box);
  const seen = new Map();
  function render() {
    box.hidden = seen.size === 0;
    box.innerHTML = '<div class="gloss-h">Abbreviations</div>' + [...seen].reverse().slice(0, 5)
      .map(([k, v]) => `<div class="gloss-r"><b>${k}</b><span>${v}</span></div>`).join("");
  }
  window.glossScan = (text) => {
    const found = (text || "").match(/\b[A-Za-z]*[A-Z][A-Za-z0-9]*\b/g) || [];
    let changed = false;
    for (let w of found) {
      let k = G[w] ? w : G[w.replace(/s$/, "")] ? w.replace(/s$/, "") : G[w.toUpperCase()] && w === w.toUpperCase() ? w.toUpperCase() : null;
      if (!k || !G[k]) continue;
      if (seen.has(k)) seen.delete(k);
      seen.set(k, G[k]); changed = true;
    }
    if (changed) render();
  };
  window.glossClear = () => { seen.clear(); render(); };
  box.addEventListener("click", () => window.glossClear());
})();
