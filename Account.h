#pragma once
/**
* &brief This Account.h conceptually acts as a namespace.
*/

/* For Contract */
#include <functional>
#include <utility> // For std::move

/* For Account */
#include <list>
#include <stdexcept> // For std::invalid_argument
#include <utility> // For std::move

/**
 * @class Contract
 */
class Contract {
public:
    explicit Contract(std::function<void()> onExpired)
        : m_onExpired(std::move(onExpired)) {}

    ~Contract() {
        if (m_onExpired) {
            m_onExpired();
        }
    }

    Contract(const Contract&) = delete;
    Contract& operator=(const Contract&) = delete;
    Contract(Contract&& other) noexcept
        : m_onExpired(std::move(other.m_onExpired)) {
         other.m_onExpired = nullptr;
    }

    Contract& operator=(Contract&& other) noexcept {
        if (this != &other) {
            if (m_onExpired) {
                m_onExpired();
            }
            m_onExpired = std::move(other.m_onExpired);
            other.m_onExpired = nullptr;
        }
        return *this;
    }

private:
    std::function<void()> m_onExpired;
};

class Account {
public:
    explicit Account(void* pOwner) : m_pOwner(pOwner) {
        if (m_pOwner == nullptr) {
            throw std::invalid_argument("Account pOwner cannot be null. Must be initialized with 'this'.");
        }
    }

    ~Account() {}

    void* getOwner() const {
        return m_pOwner;
    }

    Account(const Account&) = delete;
    Account& operator=(const Account&) = delete;
    Account(Account&&) = delete;
    Account& operator=(Account&&) = delete;

private:
    friend class IContractor;

    void _addContract(Contract&& contract) {
        m_contracts.push_back(std::move(contract));
    }

    void* m_pOwner;
    std::list<Contract> m_contracts;
};

class IContractor {
protected:
    void _addContractTo(Account& account, Contract&& contract) {
        account._addContract(std::move(contract));
    }
};
