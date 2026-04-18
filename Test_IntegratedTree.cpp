#include <iostream>
#include "CPureTree.h"
#include "Simulator.h"
#include "DomainWrappers.h"

int main() {
    nsTree::CPureTree tree;

    // -------------------------------------------------------
    // SCENARIO 1: Software Bug Propagation
    // 시나리오 1: 소프트웨어 버그 파급
    // -------------------------------------------------------
    auto core = tree.addNode(1, "Core_Engine");
    auto api  = tree.addNode(1, "Public_API");
    auto app  = tree.addNode(1, "Mobile_App");

    SoftwareEnv::connect(tree, core, api, SoftwareEnv::Constrain, 0.9f, 1.0f); // Strong coupling
    SoftwareEnv::connect(tree, api, app, SoftwareEnv::Call, 0.4f, 2.0f);      // Weak coupling, high complexity

    std::cout << "\n[Test 1] Software Bug in 'Core_Engine'";
    nsSim::Simulator::run(tree, core, 1.5f); // Impact 1.5

    // -------------------------------------------------------
    // SCENARIO 2: Data Center Power Failure
    // 시나리오 2: 데이터 센터 전력 장애 파급
    // -------------------------------------------------------
    auto ups   = tree.addNode(2, "Main_UPS");
    auto rackA = tree.addNode(2, "Server_Rack_A");
    auto db    = tree.addNode(2, "Main_DB_Cluster");

    InfraEnv::connect(tree, ups, rackA, InfraEnv::Pipe, 1.0f, 1.0f);   // Direct power line
    InfraEnv::connect(tree, rackA, db, InfraEnv::Pipe, 0.8f, 1.0f);    // Network dep

    std::cout << "\n[Test 2] Power Outage at 'Main_UPS'";
    nsSim::Simulator::run(tree, ups, 2.0f); // High shock

    // -------------------------------------------------------
    // SCENARIO 3: Security Breach (Credential Leak)
    // 시나리오 3: 보안 침해 (계정 유출 위험 확산)
    // -------------------------------------------------------
    auto adminAccount = tree.addNode(3, "Admin_Account");
    auto fileServer   = tree.addNode(3, "Confidential_Files");
    auto backupCloud  = tree.addNode(3, "Backup_Cloud");

    SecurityEnv::connect(tree, adminAccount, fileServer, SecurityEnv::AdminAccess, 1.0f, 0.5f); // High risk
    SecurityEnv::connect(tree, fileServer, backupCloud, SecurityEnv::TrustRelation, 0.6f, 1.5f); // Partial sync

    std::cout << "\n[Test 3] Credential Leak on 'Admin_Account'";
    nsSim::Simulator::run(tree, adminAccount, 1.2f); 

    return 0;
}