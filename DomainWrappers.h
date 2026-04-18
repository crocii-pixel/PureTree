#pragma once
#include "CPureTree.h"

// =========================================================
// ENVIRONMENT 1: Software Architecture (Logic & Code)
// 환경 1: 소프트웨어 아키텍처 (로직 및 소스 코드 의존성)
// =========================================================
class SoftwareEnv {
public:
    enum Type { Call = 100, Depend = 101, Constrain = 102 };
    
    static nsTree::hTree connect(nsTree::CPureTree& tree, nsTree::hTree from, nsTree::hTree to, 
                                 Type type, float coupling, float complexity = 1.0f) {
        // force = coupling (결합도), distance = complexity (복잡도)
        return tree.addLink(from, to, static_cast<int>(type), {coupling, complexity, 1.0f});
    }
};

// =========================================================
// ENVIRONMENT 2: Infrastructure & Network (Traffic & Load)
// 환경 2: 인프라 및 네트워크 (트래픽 및 부하 전이)
// =========================================================
class InfraEnv {
public:
    enum Type { Pipe = 200, Wireless = 201, Backup = 202 };

    static nsTree::hTree connect(nsTree::CPureTree& tree, nsTree::hTree from, nsTree::hTree to, 
                                 Type type, float bandwidth, float latency = 1.0f) {
        // force = bandwidth (대역폭), distance = latency (지연 시간)
        return tree.addLink(from, to, static_cast<int>(type), {bandwidth, latency, 1.0f});
    }
};

// =========================================================
// ENVIRONMENT 3: Security & Permission (Risk & Access)
// 환경 3: 보안 및 권한 (위험 확산 및 권한 오남용)
// =========================================================
class SecurityEnv {
public:
    enum Type { AdminAccess = 300, UserAccess = 301, TrustRelation = 302 };

    static nsTree::hTree connect(nsTree::CPureTree& tree, nsTree::hTree from, nsTree::hTree to, 
                                 Type type, float authority, float protection = 1.0f) {
        // force = authority (권한 세기), distance = protection (방어 수준)
        // Polarity -1.0을 사용하여 "보호막"이 에너지를 상쇄하게 설정 가능
        return tree.addLink(from, to, static_cast<int>(type), {authority, protection, 1.0f});
    }
};